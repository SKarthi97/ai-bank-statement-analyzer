"""Acceptance checks for the non-functional targets in docs/01 section 12 and requirement DB-08.

The time limits are the targets stated in the requirements ("10,000-row CSV parsed and previewed
in under 10 s", "transaction filter/sort under 1 s for 100k rows"). Measured on the development
machine they pass with a wide margin (see docs/08_ACCEPTANCE_REPORT.md); a much slower machine
could still fail them, which would be information, not a reason to loosen the limit.

Every data set here is generated and synthetic.
"""

from __future__ import annotations

import random
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from bank_analyzer.database.repositories import (
    AccountRepository,
    CategoryRepository,
    ImportRepository,
    PageRequest,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.imports import ColumnMapping, ImportChoices, SignConvention
from bank_analyzer.domain.models import AccountType, ImportBatch, MatchType, new_id, utc_now_iso
from bank_analyzer.importer import extract_table
from bank_analyzer.security.file_security import staged_upload
from bank_analyzer.services.categorization import Categorizer
from bank_analyzer.services.import_service import commit_preview, prepare_preview
from bank_analyzer.services.normalization import normalize

MERCHANTS = [
    "FRESH MART",
    "CITY POWER",
    "CAFE LUNA",
    "STREAMLY",
    "SHELL FUEL",
    "BOOKSHOP",
    "GYM CLUB",
]
MAPPING = ColumnMapping(date_col=0, description_col=1, amount_col=2, balance_col=3)


def write_statement(path: Path, rows: int, seed: int = 11) -> int:
    """A synthetic statement of ``rows`` rows. Returns the exact sum of all amounts in minor units."""
    rng = random.Random(seed)
    day = date(2015, 1, 1)
    balance = 100_000
    total = 0
    lines = ["Date,Description,Amount,Balance"]
    for i in range(rows):
        if i % 25 == 0:
            day += timedelta(days=1)
        amount = rng.randint(-30_000, 30_000) or 100
        balance += amount
        total += amount
        lines.append(
            f"{day.isoformat()},{rng.choice(MERCHANTS)} #{i} CARD 4242,{amount / 100:.2f},{balance / 100:.2f}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return total


def choices(account_id: str) -> ImportChoices:
    return ImportChoices(
        account_id=account_id,
        default_currency="USD",
        date_format="%Y-%m-%d",
        decimal_sep=".",
        thousands_sep=",",
        sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
    )


def timed(fn):  # noqa: ANN001, ANN202
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def test_a_10k_row_csv_is_parsed_previewed_and_saved_in_under_10_seconds(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    account = AccountRepository(conn).create("Main", AccountType.DEPOSIT, "USD")
    path = tmp_path / "ten_thousand.csv"
    expected_total = write_statement(path, 10_000)

    def pipeline() -> int:
        with staged_upload(path) as staged:
            raw = extract_table(staged)
        preview = prepare_preview(conn, raw, MAPPING, choices(account.id))
        summary = commit_preview(conn, preview, confirmed=True, keep_remaining_duplicates=True)
        return summary.imported

    imported, seconds = timed(pipeline)
    assert seconds < 10, f"{seconds:.1f}s"
    assert imported == 10_000
    stored = conn.execute("SELECT COUNT(*), SUM(amount_minor) FROM transactions").fetchone()
    assert (stored[0], stored[1]) == (10_000, expected_total)  # nothing lost, nothing invented


@pytest.mark.slow
def test_filtering_and_sorting_100k_rows_is_under_one_second(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    account = AccountRepository(conn).create("Main", AccountType.DEPOSIT, "USD")
    path = tmp_path / "hundred_thousand.csv"
    write_statement(path, 100_000)
    with staged_upload(path, max_mb_tabular=100) as staged:
        raw = extract_table(staged)
    batch_id = new_id()
    result = normalize(raw, MAPPING, choices(account.id), import_batch_id=batch_id)
    batch = ImportBatch(
        id=batch_id,
        account_id=account.id,
        file_sha256=raw.file_sha256,
        file_kind=raw.file_kind,
        row_count_read=result.rows_read,
        row_count_imported=len(result.transactions),
        row_count_skipped=0,
        created_at=utc_now_iso(),
    )
    ImportRepository(conn).commit_import(batch, result.transactions)
    Categorizer(conn).run(use_llm=False)
    groceries = CategoryRepository(conn).get_by_name("Groceries").id
    repo = TransactionRepository(conn)
    queries = {
        "first page by date": (TransactionFilter(), PageRequest()),
        "sorted by amount": (TransactionFilter(), PageRequest(sort_by="amount")),
        "by category": (TransactionFilter(category_id=groceries), PageRequest()),
        "text search": (TransactionFilter(text="CAFE"), PageRequest()),
        "date range": (
            TransactionFilter(date_from=date(2018, 1, 1), date_to=date(2018, 6, 30)),
            PageRequest(),
        ),
    }
    assert repo.query(TransactionFilter(), PageRequest(page_size=1)).total == 100_000
    for name, (flt, page) in queries.items():
        _, seconds = timed(lambda flt=flt, page=page: repo.query(flt, page))
        assert seconds < 1.0, f"{name}: {seconds:.2f}s"


def test_the_audit_log_never_holds_statement_content(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """DB-08: audit events carry event names, ids and counts only."""
    account = AccountRepository(conn).create("Main", AccountType.DEPOSIT, "USD")
    path = tmp_path / "s.csv"
    path.write_text(
        "Date,Description,Amount,Balance\n"
        "2024-01-02,ZEBRA SECRET MERCHANT 99887,-1234.56,8765.44\n"
        "2024-01-03,SALARY ACME PRIVATE CORP,5432.10,14197.54\n",
        encoding="utf-8",
    )
    with staged_upload(path) as staged:
        raw = extract_table(staged)
    summary = commit_preview(
        conn,
        prepare_preview(conn, raw, MAPPING, choices(account.id)),
        confirmed=True,
    )
    categorizer = Categorizer(conn)
    categorizer.run(use_llm=False)
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions")]
    categorizer.correct(ids[:1], CategoryRepository(conn).get_by_name("Shopping").id)
    categorizer.add_rule(
        match_type=MatchType.CONTAINS,
        pattern="ZEBRA",
        category_id=CategoryRepository(conn).get_by_name("Shopping").id,
    )
    ImportRepository(conn).rollback(summary.batch_id)

    rows = [tuple(r) for r in conn.execute("SELECT * FROM audit_events")]
    events = {r["event"] for r in conn.execute("SELECT event FROM audit_events")}
    assert {
        "categories_corrected",
        "import_rolled_back",
    } <= events  # the workflow really was audited
    text = " ".join(str(value) for row in rows for value in row).upper()
    for forbidden in (
        "ZEBRA",
        "SECRET",
        "ACME",
        "PRIVATE",
        "1234",
        "5432",
        "99887",
        "SALARY",
    ):
        assert forbidden not in text, forbidden
