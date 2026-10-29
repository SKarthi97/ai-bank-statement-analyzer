"""UI support code: session context, display rows, overview figures."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from assistant_data import TODAY, load_history
from bank_analyzer.ai.assistant import Assistant
from bank_analyzer.config import load_settings
from bank_analyzer.database.repositories import (
    CategoryRepository,
    PageRequest,
    TransactionRepository,
)
from bank_analyzer.domain.models import Account
from bank_analyzer.ui.context import AI_OFFLINE, open_context
from bank_analyzer.ui.display import page_summary, safe_cell, transaction_rows
from bank_analyzer.ui.overview import build_overview
from fakes import FakeOllama


def settings_in(tmp_path: Path, **extra: str):  # noqa: ANN201
    env = {"BANKAN_DATA_DIR": str(tmp_path / "data"), **extra}
    return load_settings(env, platform="win32", home=tmp_path)


class TestContext:
    def test_creates_the_database_and_a_working_assistant_connection(self, tmp_path: Path) -> None:
        ctx = open_context(settings_in(tmp_path))
        try:
            assert ctx.settings.db_path.exists()
            ctx.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
            with ctx.readonly() as ro:
                Assistant(ro, TODAY)  # accepted: it is read-only
                with pytest.raises(sqlite3.OperationalError):
                    ro.execute("DELETE FROM transactions")
        finally:
            ctx.close()

    def test_a_cloud_synced_data_folder_is_flagged(self, tmp_path: Path) -> None:
        ctx = open_context(settings_in(tmp_path / "OneDrive - Contoso"))
        try:
            assert ctx.sync_warning is not None
        finally:
            ctx.close()

    def test_ai_offline_when_nothing_is_listening(self, tmp_path: Path) -> None:
        ctx = open_context(settings_in(tmp_path, BANKAN_OLLAMA_URL="http://127.0.0.1:9"))
        try:
            status = ctx.ai_status()
            assert (status.available, status.message) == (False, AI_OFFLINE)
        finally:
            ctx.close()

    def test_ai_ready_when_the_model_is_installed(self, tmp_path: Path) -> None:
        with FakeOllama(models=("llama3.2:3b",)) as server:
            ctx = open_context(settings_in(tmp_path, BANKAN_OLLAMA_URL=server.url))
            try:
                assert ctx.ai_status().available
            finally:
                ctx.close()

    def test_ai_offline_when_the_model_is_missing(self, tmp_path: Path) -> None:
        with FakeOllama(models=("other:1b",)) as server:
            ctx = open_context(settings_in(tmp_path, BANKAN_OLLAMA_URL=server.url))
            try:
                assert not ctx.ai_status().available
            finally:
                ctx.close()


class TestDisplay:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, ""),
            ("", ""),
            ("plain text", "plain text"),
            ("a\x00b\x1b[31mred\x07", "a b [31mred"),
            ("line1\nline2\r\n\tline3", "line1 line2 line3"),
            ("zero​width", "zero width"),
            ("  many   spaces  ", "many spaces"),
        ],
    )
    def test_safe_cell(self, raw: str | None, expected: str) -> None:
        assert safe_cell(raw) == expected

    def test_safe_cell_bounds_length(self) -> None:
        out = safe_cell("x" * 1000, 50)
        assert len(out) == 50
        assert out.endswith("…")

    def test_transaction_rows(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        load_history(conn, usd_account)
        categories = {c.id: c for c in CategoryRepository(conn).list(active_only=False)}
        page = TransactionRepository(conn).query(page=PageRequest(page_size=3, descending=False))
        rows = transaction_rows(page.items, categories)
        assert rows[0] == {
            "Date": "2024-01-02",
            "Description": "ACME PAYROLL",
            "Amount": "3,000.00 USD",
            "Category": "Salary",
            "Status": "approved",
            "Source": "user manual",
            "Transfer": "",
            "ID": page.items[0].id,
        }
        assert page_summary(page) == "Showing 1-3 of 15"

    def test_empty_page_summary(self, conn: sqlite3.Connection) -> None:
        assert page_summary(TransactionRepository(conn).query()) == "No transactions match."


class TestOverview:
    def test_counts_and_last_month(self, conn: sqlite3.Connection, usd_account: Account) -> None:
        load_history(conn, usd_account)
        overview = build_overview(conn, TODAY)
        assert (overview.accounts, overview.transactions, overview.imports) == (1, 15, 1)
        assert overview.needs_review == 2  # the two BOOKSHOP rows
        assert overview.open_alerts == 0
        assert overview.last_month.start == date(2024, 2, 1)
        (summary,) = overview.last_month_summaries
        assert (summary.currency, summary.expenses.minor) == ("USD", 139999)

    def test_empty_database(self, conn: sqlite3.Connection) -> None:
        overview = build_overview(conn, TODAY)
        assert (overview.transactions, overview.last_month_summaries) == (0, ())


class TestAvailableMonths:
    def test_lists_months_with_data_newest_first(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        from bank_analyzer.ui.overview import available_months

        load_history(conn, usd_account)
        assert available_months(conn) == ["2024-03", "2024-02", "2024-01"]

    def test_empty(self, conn: sqlite3.Connection) -> None:
        from bank_analyzer.ui.overview import available_months

        assert available_months(conn) == []
