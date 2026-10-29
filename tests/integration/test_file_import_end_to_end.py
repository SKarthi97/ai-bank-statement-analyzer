"""Real files -> staging -> extraction -> mapping -> normalization -> SQLite -> oracle totals."""

from __future__ import annotations

import sqlite3
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
    TransactionRepository,
)
from bank_analyzer.domain.errors import FileValidationError
from bank_analyzer.domain.imports import ImportChoices, NormalizationResult, SignConvention
from bank_analyzer.domain.models import AccountType, ImportBatch, new_id, utc_now_iso
from bank_analyzer.domain.money import sum_money
from bank_analyzer.importer import extract_table
from bank_analyzer.importer.column_mapper import infer_sign_convention, suggest_mapping
from bank_analyzer.security.file_security import staged_upload
from bank_analyzer.services.normalization import detect_formats, normalize
from helpers import FIXTURES, expected


def run_pipeline(
    conn: sqlite3.Connection, filename: str, currency: str, *, source: Path | None = None
) -> tuple[NormalizationResult, ImportBatch]:
    """Drive the same steps the Import Center will: nothing is guessed, ambiguity fails."""
    account = AccountRepository(conn).create(f"acct-{new_id()[:6]}", AccountType.DEPOSIT, currency)
    path = source or FIXTURES / filename
    with staged_upload(path) as staged:
        raw = extract_table(staged)
    suggestion = suggest_mapping(raw.headers)
    assert suggestion.mapping is not None, suggestion.notes
    mapping = suggestion.mapping
    detection = detect_formats(raw, mapping, currency=currency)
    assert not detection.needs_user_choice, detection
    fmt = detection.number.candidates[0]
    if mapping.amount_col is not None:
        hint = infer_sign_convention(
            raw,
            mapping,
            currency=currency,
            decimal_sep=fmt.decimal_sep,
            thousands_sep=fmt.thousands_sep,
            account_type=AccountType.DEPOSIT,
        )
        assert hint.convention is not None, hint.note
        convention = hint.convention
    else:
        convention = SignConvention.DEBIT_CREDIT_COLUMNS
    choices = ImportChoices(
        account_id=account.id,
        default_currency=currency,
        date_format=detection.date.candidates[0],
        decimal_sep=fmt.decimal_sep,
        thousands_sep=fmt.thousands_sep,
        sign_convention=convention,
    )
    batch_id = new_id()
    result = normalize(raw, mapping, choices, import_batch_id=batch_id)
    batch = ImportBatch(
        id=batch_id,
        account_id=account.id,
        file_sha256=raw.file_sha256,
        file_kind=raw.file_kind,
        display_name=filename,
        row_count_read=result.rows_read,
        row_count_imported=len(result.transactions),
        row_count_skipped=result.rows_skipped + result.rows_rejected,
        created_at=utc_now_iso(),
    )
    return result, batch


def totals(result: NormalizationResult, currency: str) -> tuple[Decimal, Decimal]:
    credits = sum_money((t.amount for t in result.transactions if t.amount_minor > 0), currency)
    debits = sum_money((t.amount for t in result.transactions if t.amount_minor < 0), currency)
    return credits.to_decimal(), debits.to_decimal()


@pytest.mark.parametrize(
    ("filename", "oracle", "currency"),
    [
        ("deposit_basic.csv", "deposit_basic.expected.json", "USD"),
        ("bank_basic.xlsx", "deposit_basic.expected.json", "USD"),  # same data, other format
        ("deposit_eu.csv", "deposit_eu.expected.json", "EUR"),
        ("text_amounts_eu.xlsx", "deposit_eu.expected.json", "EUR"),
        ("bank_preamble_footer.csv", "bank_preamble_footer.expected.json", "USD"),
        ("statement_ruled.pdf", "deposit_basic.expected.json", "USD"),  # two PDF pages
        ("statement_debit_credit.pdf", "bank_preamble_footer.expected.json", "USD"),
    ],
)
def test_file_totals_match_independent_oracle(
    conn: sqlite3.Connection, filename: str, oracle: str, currency: str
) -> None:
    want = expected(oracle)
    result, batch = run_pipeline(conn, filename, currency)
    assert not result.has_errors
    assert len(result.transactions) == want["row_count"]
    assert totals(result, currency) == (
        Decimal(want["total_credits"]),
        Decimal(want["total_debits"]),
    )

    # and the same numbers must come back out of the database
    ImportRepository(conn).commit_import(batch, result.transactions)
    stored = TransactionRepository(conn).monthly_totals()
    assert sum(m.credits.minor for m in stored) == int(Decimal(want["total_credits"]) * 100)
    assert sum(m.debits.minor for m in stored) == int(Decimal(want["total_debits"]) * 100)


def test_csv_and_xlsx_of_the_same_statement_agree_row_by_row(conn: sqlite3.Connection) -> None:
    csv_result, _ = run_pipeline(conn, "deposit_basic.csv", "USD")
    xlsx_result, _ = run_pipeline(conn, "bank_basic.xlsx", "USD")
    key = lambda t: (t.txn_date, t.amount_minor, t.normalized_description, t.reported_balance_minor)  # noqa: E731
    assert [key(t) for t in csv_result.transactions] == [key(t) for t in xlsx_result.transactions]


def test_multiline_description_and_footer_handled(conn: sqlite3.Connection) -> None:
    want = expected("bank_preamble_footer.expected.json")
    result, _ = run_pipeline(conn, "bank_preamble_footer.csv", "USD")
    assert [t.original_description for t in result.transactions][1] == want["multiline_description"]
    assert result.issue_counts() == {"I_FOOTER_SKIPPED": 1}


def test_source_rows_point_back_into_the_file(conn: sqlite3.Connection) -> None:
    result, _ = run_pipeline(conn, "bank_preamble_footer.csv", "USD")
    # Numbers are CSV *records*, not editor lines (a quoted two-line cell is one record):
    # 1-3 preamble, 4 blank, 5 header, 6 rent, 7 supermarket, 8 blank, 9 payment, 10 totals
    assert [t.source_ref.row_index for t in result.transactions] == [6, 7, 9]


def test_xlsx_rows_carry_sheet_and_row_number(conn: sqlite3.Connection) -> None:
    result, _ = run_pipeline(conn, "bank_basic.xlsx", "USD")
    first = result.transactions[0].source_ref
    assert (first.location, first.row_index) == ("Transactions", 5)


def test_float_noise_in_excel_is_not_silently_rounded(conn: sqlite3.Connection) -> None:
    """The amount 0.30000000000000004 has no valid reading, so nothing may be assumed."""
    path = FIXTURES / "float_noise.xlsx"
    with staged_upload(path) as staged:
        raw = extract_table(staged)
    mapping = suggest_mapping(raw.headers).mapping
    assert mapping is not None
    assert detect_formats(raw, mapping, currency="USD").needs_user_choice
    account = AccountRepository(conn).create("A", AccountType.DEPOSIT, "USD")
    result = normalize(
        raw,
        mapping,
        ImportChoices(
            account_id=account.id,
            default_currency="USD",
            date_format="%Y-%m-%d",
            decimal_sep=".",
            thousands_sep=",",
            sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
        ),
        import_batch_id=new_id(),
    )
    assert [t.amount_minor for t in result.transactions] == [-1025]  # only the clean row
    assert result.issue_counts() == {"E_BAD_AMOUNT": 1} and result.rows_rejected == 1


def test_ambiguous_dates_stop_the_pipeline_until_user_chooses(tmp_path: Path) -> None:
    path = tmp_path / "ambiguous.csv"
    path.write_text("Date,Description,Amount\n01/02/2024,A,-1.00\n03/04/2024,B,-2.00\n")
    with staged_upload(path) as staged:
        raw = extract_table(staged)
    mapping = suggest_mapping(raw.headers).mapping
    assert mapping is not None
    detection = detect_formats(raw, mapping, currency="USD")
    assert detection.date.ambiguous and detection.needs_user_choice
    assert set(detection.date.candidates) == {"%d/%m/%Y", "%m/%d/%Y"}


def test_hostile_files_never_reach_the_database(conn: sqlite3.Connection, tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/worksheets/sheet1.xml", b"\x00" * 5_000_000)
    disguised = tmp_path / "invoice.csv"
    disguised.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")
    for hostile in (bomb, disguised):
        with pytest.raises(FileValidationError), staged_upload(hostile):
            pass
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0


def test_import_then_rollback_restores_state(conn: sqlite3.Connection) -> None:
    result, batch = run_pipeline(conn, "bank_basic.xlsx", "USD")
    imports = ImportRepository(conn)
    imports.commit_import(batch, result.transactions)
    assert imports.find_by_hash(batch.file_sha256)
    imports.rollback(batch.id)
    assert TransactionRepository(conn).monthly_totals() == []


def test_original_files_are_untouched_by_the_pipeline(conn: sqlite3.Connection) -> None:
    snapshot = {p: p.read_bytes() for p in FIXTURES.iterdir() if p.suffix in {".csv", ".xlsx"}}
    for name, ccy in (("deposit_basic.csv", "USD"), ("bank_basic.xlsx", "USD")):
        run_pipeline(conn, name, ccy)
    assert snapshot == {p: p.read_bytes() for p in snapshot}


def test_pdf_and_csv_of_the_same_statement_agree_row_by_row(conn: sqlite3.Connection) -> None:
    csv_result, _ = run_pipeline(conn, "deposit_basic.csv", "USD")
    pdf_result, _ = run_pipeline(conn, "statement_ruled.pdf", "USD")
    key = lambda t: (t.txn_date, t.amount_minor, t.normalized_description, t.reported_balance_minor)  # noqa: E731
    assert [key(t) for t in csv_result.transactions] == [key(t) for t in pdf_result.transactions]


def test_pdf_rows_point_back_to_page_and_row(conn: sqlite3.Connection) -> None:
    result, _ = run_pipeline(conn, "statement_ruled.pdf", "USD")
    refs = [(t.source_ref.location, t.source_ref.row_index) for t in result.transactions]
    assert refs[0] == ("page 1", 2) and refs[5] == ("page 2", 2)


def test_pdf_totals_row_is_skipped_not_imported(conn: sqlite3.Connection) -> None:
    result, _ = run_pipeline(conn, "statement_debit_credit.pdf", "USD")
    assert len(result.transactions) == 3 and result.issue_counts() == {"I_FOOTER_SKIPPED": 1}


def test_scanned_pdf_asks_for_ocr_and_ocr_reports_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bank_analyzer.domain.errors import ImportStructureError, NeedsOcrError
    from bank_analyzer.importer import ocr_importer

    with staged_upload(FIXTURES / "image_only.pdf") as staged:
        with pytest.raises(NeedsOcrError):
            extract_table(staged)
        monkeypatch.setattr(ocr_importer, "find_tesseract", lambda command=None: None)
        with pytest.raises(ImportStructureError, match="Tesseract is not installed"):
            extract_table(staged, use_ocr=True)


def test_full_pdf_workflow_through_the_import_service(conn: sqlite3.Connection) -> None:
    """staged PDF -> extract -> preview -> confirm -> stored totals equal the oracle."""
    from bank_analyzer.services.import_service import commit_preview, prepare_preview

    want = expected("deposit_basic.expected.json")
    account = AccountRepository(conn).create("PDF acct", AccountType.DEPOSIT, "USD")
    with staged_upload(FIXTURES / "statement_ruled.pdf") as staged:
        raw = extract_table(staged)
    mapping = suggest_mapping(raw.headers).mapping
    assert mapping is not None
    choices = ImportChoices(
        account_id=account.id,
        default_currency="USD",
        date_format="%Y-%m-%d",
        decimal_sep=".",
        thousands_sep=",",
        sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
    )
    preview = prepare_preview(conn, raw, mapping, choices, display_name="statement_ruled.pdf")
    assert not preview.duplicates and not preview.needs_rejected_ack
    commit_preview(conn, preview, confirmed=True)
    stored = TransactionRepository(conn).monthly_totals()
    assert sum(m.credits.minor for m in stored) == int(Decimal(want["total_credits"]) * 100)
    assert sum(m.debits.minor for m in stored) == int(Decimal(want["total_debits"]) * 100)
