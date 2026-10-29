from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bank_analyzer.database.repositories import (
    AccountRepository,
    ImportRepository,
    TransactionRepository,
)
from bank_analyzer.domain.errors import InvalidStateError, NotFoundError
from bank_analyzer.domain.imports import (
    ColumnMapping,
    ImportChoices,
    RawRow,
    RawTable,
    SignConvention,
)
from bank_analyzer.domain.models import AccountType, FileKind, new_id
from bank_analyzer.services.import_service import (
    ImportPreview,
    commit_preview,
    prepare_preview,
)
from helpers import raw_from_rows

MAPPING = ColumnMapping(date_col=0, description_col=1, amount_col=2)
TODAY = date(2025, 1, 1)


@pytest.fixture
def account_id(conn: sqlite3.Connection) -> str:
    return AccountRepository(conn).create("Checking", AccountType.DEPOSIT, "USD").id


def choices(account_id: str) -> ImportChoices:
    return ImportChoices(
        account_id=account_id,
        default_currency="USD",
        date_format="%Y-%m-%d",
        decimal_sep=".",
        thousands_sep=",",
        sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
    )


def preview(
    conn: sqlite3.Connection, account_id: str, rows: list[list[str]], sha: str = "0" * 64
) -> ImportPreview:
    raw = raw_from_rows(rows)
    raw = RawTable(
        raw.file_sha256 if sha == "0" * 64 else sha, raw.file_kind, raw.headers, raw.rows
    )
    return prepare_preview(
        conn, raw, MAPPING, choices(account_id), display_name="s.csv", today=TODAY
    )


ROWS = [
    ["2024-01-02", "SALARY", "3000.00"],
    ["2024-01-03", "GROCERY MART", "-85.40"],
    ["2024-01-04", "POWER", "-120.00"],
]


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])


class TestPreviewDoesNotSave:
    def test_nothing_is_written(self, conn: sqlite3.Connection, account_id: str) -> None:
        p = preview(conn, account_id, ROWS)
        assert len(p.result.transactions) == 3
        assert count(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0

    def test_unknown_account_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(NotFoundError):
            preview(conn, "no-such-account", ROWS)

    def test_clean_import_needs_no_acknowledgements(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        p = preview(conn, account_id, ROWS)
        assert not (
            p.needs_rejected_ack
            or p.needs_low_confidence_ack
            or p.needs_reimport_ack
            or p.needs_duplicate_decision
        )
        summary = commit_preview(conn, p, confirmed=True)
        assert summary.imported == 3 and count(conn) == 3


class TestConfirmationGates:
    def test_commit_requires_explicit_confirmation(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        with pytest.raises(InvalidStateError, match="not confirmed"):
            commit_preview(conn, preview(conn, account_id, ROWS), confirmed=False)
        assert count(conn) == 0

    def test_rejected_rows_must_be_acknowledged(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        p = preview(conn, account_id, [*ROWS, ["not-a-date", "BAD", "1.00"]])
        assert p.needs_rejected_ack and p.result.rows_rejected == 1
        with pytest.raises(InvalidStateError, match="rejected"):
            commit_preview(conn, p, confirmed=True)
        assert count(conn) == 0
        summary = commit_preview(conn, p, confirmed=True, accept_rejected_rows=True)
        assert summary.imported == 3

    def test_low_confidence_ocr_amounts_must_be_confirmed(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        rows = (
            RawRow(2, ("2024-01-02", "SALARY", "3000.00"), "page 1", confidence=95.0),
            RawRow(3, ("2024-01-03", "SHOP", "-85.40"), "page 1", confidence=41.0),
        )
        raw = RawTable("1" * 64, FileKind.PDF_OCR, ("Date", "Description", "Amount"), rows)
        p = prepare_preview(conn, raw, MAPPING, choices(account_id), today=TODAY)
        assert p.needs_low_confidence_ack and p.result.low_confidence_rows == (3,)
        with pytest.raises(InvalidStateError, match="OCR confidence"):
            commit_preview(conn, p, confirmed=True)
        assert count(conn) == 0
        assert (
            commit_preview(conn, p, confirmed=True, confirm_low_confidence_amounts=True).imported
            == 2
        )

    def test_importing_the_same_file_again_needs_explicit_permission(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        commit_preview(conn, preview(conn, account_id, ROWS), confirmed=True)
        again = preview(conn, account_id, ROWS)
        assert again.needs_reimport_ack and len(again.prior_imports) == 1
        with pytest.raises(InvalidStateError, match="already imported"):
            commit_preview(conn, again, confirmed=True, keep_remaining_duplicates=True)

    def test_a_rolled_back_file_may_be_imported_again_freely(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        first = commit_preview(conn, preview(conn, account_id, ROWS), confirmed=True)
        ImportRepository(conn).rollback(first.batch_id)
        again = preview(conn, account_id, ROWS)
        assert not again.needs_reimport_ack and not again.duplicates
        assert commit_preview(conn, again, confirmed=True).imported == 3


class TestDuplicates:
    def test_exact_reimport_flags_every_row_and_blocks_until_decided(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        commit_preview(conn, preview(conn, account_id, ROWS), confirmed=True)
        again = preview(conn, account_id, ROWS)
        assert len(again.duplicates) == 3 and again.needs_duplicate_decision
        with pytest.raises(InvalidStateError, match="suspected duplicate"):
            commit_preview(conn, again, confirmed=True, allow_reimport=True)
        assert count(conn) == 3

    def test_skipping_duplicates_imports_nothing_and_changes_nothing_stored(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        commit_preview(conn, preview(conn, account_id, ROWS), confirmed=True)
        before = conn.execute(
            "SELECT id, amount_minor, updated_at FROM transactions ORDER BY id"
        ).fetchall()
        again = preview(conn, account_id, ROWS)
        summary = commit_preview(
            conn,
            again,
            confirmed=True,
            allow_reimport=True,
            skip_transaction_ids={d.transaction_id for d in again.duplicates},
        )
        assert summary.imported == 0
        after = conn.execute(
            "SELECT id, amount_minor, updated_at FROM transactions ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]  # nothing deleted or edited

    def test_keeping_duplicates_is_possible_but_explicit(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        commit_preview(conn, preview(conn, account_id, ROWS), confirmed=True)
        again = preview(conn, account_id, ROWS)
        summary = commit_preview(
            conn, again, confirmed=True, allow_reimport=True, keep_remaining_duplicates=True
        )
        assert summary.imported == 3 and count(conn) == 6

    def test_overlapping_statements_flag_only_the_overlap(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        rows = [[f"2024-01-{d:02d}", f"ITEM {d}", f"-{d}.00"] for d in range(1, 8)]
        commit_preview(conn, preview(conn, account_id, rows[:5], sha="a" * 64), confirmed=True)
        second = preview(conn, account_id, rows[3:], sha="b" * 64)  # rows 4-7, rows 4-5 overlap
        flagged_days = sorted(
            t.txn_date.day
            for t in second.result.transactions
            if t.id in {d.transaction_id for d in second.duplicates}
        )
        assert flagged_days == [4, 5]
        summary = commit_preview(
            conn,
            second,
            confirmed=True,
            skip_transaction_ids={d.transaction_id for d in second.duplicates},
        )
        assert summary.imported == 2 and count(conn) == 7

    def test_legitimate_repeats_beyond_the_stored_count_are_not_flagged(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        coffee = ["2024-01-02", "COFFEE", "-3.00"]
        commit_preview(conn, preview(conn, account_id, [coffee], sha="a" * 64), confirmed=True)
        p = preview(conn, account_id, [coffee, coffee, coffee], sha="b" * 64)
        assert len(p.duplicates) == 1  # one is already stored; two are genuinely new
        assert p.identical_rows_in_file == 2
        flagged = p.duplicates[0].transaction_id
        assert flagged == p.result.transactions[0].id  # the first one matches the stored row

    def test_rolled_back_rows_are_not_duplicates(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        first = commit_preview(conn, preview(conn, account_id, ROWS, sha="a" * 64), confirmed=True)
        ImportRepository(conn).rollback(first.batch_id)
        assert preview(conn, account_id, ROWS, sha="b" * 64).duplicates == ()

    def test_different_accounts_are_not_duplicates(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        other = AccountRepository(conn).create("Savings", AccountType.DEPOSIT, "USD").id
        commit_preview(conn, preview(conn, account_id, ROWS, sha="a" * 64), confirmed=True)
        assert preview(conn, other, ROWS, sha="b" * 64).duplicates == ()

    def test_skip_ids_must_belong_to_this_import(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        with pytest.raises(InvalidStateError, match="not part of this import"):
            commit_preview(
                conn,
                preview(conn, account_id, ROWS),
                confirmed=True,
                skip_transaction_ids={new_id()},
            )

    def test_user_skips_are_recorded_in_the_batch(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        p = preview(conn, account_id, ROWS)
        skipped = {p.result.transactions[0].id}
        summary = commit_preview(conn, p, confirmed=True, skip_transaction_ids=skipped)
        batch = ImportRepository(conn).get(summary.batch_id)
        assert batch.row_count_imported == 2 and batch.row_count_skipped == 1
        assert "ROWS_SKIPPED_BY_USER x1" in batch.warnings
        assert count(conn) == 2


class TestBatchBookkeeping:
    def test_batch_warnings_contain_codes_only(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        p = preview(conn, account_id, [["2024-01-02", "SECRET SHOP", "0.00"]])
        summary = commit_preview(conn, p, confirmed=True)
        warnings = ImportRepository(conn).get(summary.batch_id).warnings
        assert warnings == ("W_ZERO_AMOUNT x1",)
        assert all("SECRET" not in w for w in warnings)

    def test_stored_transactions_are_queryable_and_voidable(
        self, conn: sqlite3.Connection, account_id: str
    ) -> None:
        summary = commit_preview(conn, preview(conn, account_id, ROWS), confirmed=True)
        txns = TransactionRepository(conn)
        assert txns.query().total == 3
        ImportRepository(conn).rollback(summary.batch_id)
        assert txns.query().total == 0
