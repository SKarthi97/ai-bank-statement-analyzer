"""End to end: synthetic CSV -> RawTable -> normalize -> SQLite -> totals vs. hand-computed oracle."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.imports import ColumnMapping, ImportChoices, SignConvention
from bank_analyzer.domain.models import AccountType, FileKind, ImportBatch, new_id, utc_now_iso
from bank_analyzer.domain.money import Money, sum_money
from bank_analyzer.services.normalization import detect_formats, normalize
from helpers import expected, raw_from_csv


def import_basic(conn: sqlite3.Connection) -> tuple[ImportBatch, str]:
    accounts = AccountRepository(conn)
    account = accounts.create("Checking", AccountType.DEPOSIT, "USD")
    raw = raw_from_csv("deposit_basic.csv")
    mapping = ColumnMapping(date_col=0, description_col=1, amount_col=2, balance_col=3)
    detection = detect_formats(raw, mapping, currency="USD")
    assert not detection.needs_user_choice  # ISO dates + US numbers are unambiguous
    choices = ImportChoices(
        account_id=account.id,
        default_currency="USD",
        date_format=detection.date.candidates[0],
        decimal_sep=detection.number.candidates[0].decimal_sep,
        thousands_sep=detection.number.candidates[0].thousands_sep,
        sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
    )
    batch_id = new_id()
    result = normalize(raw, mapping, choices, import_batch_id=batch_id)
    assert not result.has_errors
    batch = ImportBatch(
        id=batch_id,
        account_id=account.id,
        file_sha256=raw.file_sha256,
        file_kind=FileKind.CSV,
        display_name="deposit_basic.csv",
        row_count_read=result.rows_read,
        row_count_imported=len(result.transactions),
        row_count_skipped=result.rows_skipped + result.rows_rejected,
        warnings=tuple(f"{c} x{n}" for c, n in result.issue_counts().items()),
        created_at=utc_now_iso(),
    )
    ImportRepository(conn).commit_import(batch, result.transactions)
    return batch, account.id


def test_monthly_totals_match_independent_oracle(conn: sqlite3.Connection) -> None:
    import_basic(conn)
    oracle = expected("deposit_basic.expected.json")
    totals = TransactionRepository(conn).monthly_totals()

    assert {t.month for t in totals} == set(oracle["months"])
    for t in totals:
        want = oracle["months"][t.month]
        assert t.credits.to_decimal() == Decimal(want["credits"])
        assert t.debits.to_decimal() == Decimal(want["debits"])
        assert t.count == want["count"]

    credits = sum_money((t.credits for t in totals), "USD")
    debits = sum_money((t.debits for t in totals), "USD")
    assert credits.to_decimal() == Decimal(oracle["total_credits"])
    assert debits.to_decimal() == Decimal(oracle["total_debits"])
    assert (credits + debits).to_decimal() == Decimal(oracle["net"])


def test_all_rows_stored_including_identical_pair(conn: sqlite3.Connection) -> None:
    import_basic(conn)
    page = TransactionRepository(conn).query()
    assert page.total == expected("deposit_basic.expected.json")["row_count"]
    twins = [
        t
        for t in page.items
        if t.normalized_description == "GROCERY MART" and t.amount_minor == -9210
    ]
    assert sorted(t.occurrence_index for t in twins) == [0, 1]


def test_reported_balances_reconcile_with_amounts(conn: sqlite3.Connection) -> None:
    """Independent check of the oracle: each balance equals the previous balance plus amount."""
    import_basic(conn)
    rows = TransactionRepository(conn).query().items
    rows.sort(key=lambda t: t.source_ref.row_index)
    balance = Money(50000, "USD")  # opening balance from the fixture note
    for t in rows:
        balance = balance + t.amount
        assert t.reported_balance == balance
    assert balance.to_decimal() == Decimal(
        expected("deposit_basic.expected.json")["closing_balance"]
    )


def test_traceability_to_source_rows(conn: sqlite3.Connection) -> None:
    batch, _ = import_basic(conn)
    rows = TransactionRepository(conn).query(TransactionFilter(batch_id=batch.id)).items
    assert sorted(t.source_ref.row_index for t in rows) == list(range(2, 11))
    assert {t.source_ref.file_sha256 for t in rows} == {batch.file_sha256}


def test_rollback_restores_pre_import_state(conn: sqlite3.Connection) -> None:
    txns = TransactionRepository(conn)
    batch, _ = import_basic(conn)
    assert txns.monthly_totals()
    ImportRepository(conn).rollback(batch.id)
    assert txns.monthly_totals() == []
    assert txns.query().total == 0


def test_reimport_of_same_file_is_detectable(conn: sqlite3.Connection) -> None:
    batch, _ = import_basic(conn)
    assert [b.id for b in ImportRepository(conn).find_by_hash(batch.file_sha256)] == [batch.id]


def test_european_statement_with_separate_debit_credit_columns(conn: sqlite3.Connection) -> None:
    account = AccountRepository(conn).create("Giro", AccountType.DEPOSIT, "EUR")
    raw = raw_from_csv("deposit_eu.csv", delimiter=";")
    mapping = ColumnMapping(date_col=0, description_col=1, debit_col=2, credit_col=3, balance_col=4)
    detection = detect_formats(raw, mapping, currency="EUR")
    # 25.01.2024 forces day-first; decimal comma / thousands dot is the only valid reading
    assert detection.date.candidates == ("%d.%m.%Y",) and not detection.needs_user_choice
    fmt = detection.number.candidates[0]
    assert (fmt.decimal_sep, fmt.thousands_sep) == (",", ".")

    choices = ImportChoices(
        account_id=account.id,
        default_currency="EUR",
        date_format="%d.%m.%Y",
        decimal_sep=fmt.decimal_sep,
        thousands_sep=fmt.thousands_sep,
        sign_convention=SignConvention.DEBIT_CREDIT_COLUMNS,
    )
    result = normalize(raw, mapping, choices, import_batch_id=new_id())
    oracle = expected("deposit_eu.expected.json")
    assert not result.issues and len(result.transactions) == oracle["row_count"]
    credits = sum_money((t.amount for t in result.transactions if t.amount_minor > 0), "EUR")
    debits = sum_money((t.amount for t in result.transactions if t.amount_minor < 0), "EUR")
    assert credits.to_decimal() == Decimal(oracle["total_credits"])
    assert debits.to_decimal() == Decimal(oracle["total_debits"])
    assert result.transactions[-1].reported_balance.to_decimal() == Decimal(  # type: ignore[union-attr]
        oracle["closing_balance"]
    )
