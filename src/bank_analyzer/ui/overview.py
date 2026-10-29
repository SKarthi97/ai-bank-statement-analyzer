"""Figures for the Overview page."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from bank_analyzer.database.repositories import (
    AccountRepository,
    AlertRepository,
    ImportRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.models import AlertStatus, BatchStatus
from bank_analyzer.domain.periods import DateRange
from bank_analyzer.services.analytics import Analyzer, CashFlowSummary


@dataclass(frozen=True, slots=True)
class Overview:
    accounts: int
    transactions: int
    imports: int
    needs_review: int
    open_alerts: int
    last_month: DateRange
    last_month_summaries: tuple[CashFlowSummary, ...]  # one per currency; never mixed


def build_overview(conn: sqlite3.Connection, today: date) -> Overview:
    txns = TransactionRepository(conn)
    period = DateRange.last_n_months(today, 1)
    analyzer = Analyzer.from_db(conn)
    summaries = tuple(analyzer.summary(period, c) for c in analyzer.currencies(period))
    batches = [
        b for b in ImportRepository(conn).list_batches() if b.status == BatchStatus.COMMITTED
    ]
    return Overview(
        accounts=len(AccountRepository(conn).list()),
        transactions=len(txns.fetch_all(TransactionFilter())),
        imports=len(batches),
        needs_review=len(txns.list_pending_categorization()),
        open_alerts=len(AlertRepository(conn).list_alerts(status=AlertStatus.OPEN)),
        last_month=period,
        last_month_summaries=summaries,
    )


def available_months(conn: sqlite3.Connection) -> list[str]:
    """Calendar months (``YYYY-MM``) that have transactions, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions WHERE is_voided = 0 "
        "ORDER BY 1 DESC"
    ).fetchall()
    return [r[0] for r in rows]
