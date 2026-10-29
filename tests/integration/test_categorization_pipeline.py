"""Import a synthetic statement, categorize it, and check results against hand-computed answers."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from bank_analyzer.ai.local_llm import OllamaClient
from bank_analyzer.ai.selftest import run_selftest
from bank_analyzer.database.repositories import (
    AccountRepository,
    CategoryRepository,
    ImportRepository,
    TransactionRepository,
)
from bank_analyzer.domain.imports import ColumnMapping, ImportChoices, SignConvention
from bank_analyzer.domain.models import Account, ImportBatch, new_id, utc_now_iso
from bank_analyzer.services.categorization import Categorizer
from bank_analyzer.services.normalization import normalize
from fakes import DownLLM, FakeOllama, KeywordLLM, load_transactions
from helpers import raw_from_csv

MAPPING = ColumnMapping(date_col=0, description_col=1, amount_col=2, balance_col=3)


def import_basic_statement(conn: sqlite3.Connection, account: Account) -> list[str]:
    raw = raw_from_csv("deposit_basic.csv")
    choices = ImportChoices(
        account_id=account.id,
        default_currency="USD",
        date_format="%Y-%m-%d",
        decimal_sep=".",
        thousands_sep=",",
        sign_convention=SignConvention.NEGATIVE_IS_OUTFLOW,
    )
    batch_id = new_id()
    result = normalize(raw, MAPPING, choices, import_batch_id=batch_id)
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
    return [t.id for t in result.transactions]


def totals_by_category(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Independent of the application's own aggregation: plain SQL over minor units."""
    rows = conn.execute(
        "SELECT COALESCE(c.name, '(uncategorized)') AS name, SUM(t.amount_minor) AS total "
        "FROM transactions t LEFT JOIN categories c ON c.id = t.category_id "
        "WHERE t.is_voided = 0 GROUP BY name"
    ).fetchall()
    return {r["name"]: Decimal(r["total"]).scaleb(-2) for r in rows}


# Hand-computed from deposit_basic.csv (see deposit_basic.expected.json for the raw rows):
#   Salary 3000.00 + 3000.00; Groceries -(85.40 + 92.10 + 92.10); Utilities -120.00;
#   Subscriptions -15.99; Cash withdrawals -200.00; Refunds +12.50
EXPECTED_CATEGORY_TOTALS = {
    "Salary": Decimal("6000.00"),
    "Groceries": Decimal("-269.60"),
    "Utilities": Decimal("-120.00"),
    "Subscriptions": Decimal("-15.99"),
    "Cash withdrawals": Decimal("-200.00"),
    "Refunds": Decimal("12.50"),
}


class TestRuleOnlyCategorization:
    def test_every_row_of_the_statement_is_categorized_correctly_by_rules(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_basic_statement(conn, usd_account)
        report = Categorizer(conn, llm=None).run()
        assert report.by_source == {"builtin_rule": 9} and report.remaining_uncategorized == 0
        assert totals_by_category(conn) == EXPECTED_CATEGORY_TOTALS

    def test_refund_is_a_refund_not_groceries(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        """Regression: 'REFUND' used to be stripped from descriptions as a reference token."""
        import_basic_statement(conn, usd_account)
        Categorizer(conn).run()
        refund = [
            t
            for t in TransactionRepository(conn).query().items
            if t.original_description == "REFUND GROCERY MART"
        ][0]
        assert refund.normalized_description == "REFUND GROCERY MART"
        assert CategoryRepository(conn).get(refund.category_id or "").name == "Refunds"

    def test_categorizing_does_not_change_any_amount_or_total(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_basic_statement(conn, usd_account)
        txns = TransactionRepository(conn)
        before = [(m.month, m.credits.minor, m.debits.minor) for m in txns.monthly_totals()]
        Categorizer(conn, KeywordLLM()).run()
        after = [(m.month, m.credits.minor, m.debits.minor) for m in txns.monthly_totals()]
        assert before == after == [("2024-01", 300000, -22139), ("2024-02", 301250, -38420)]

    def test_identical_legitimate_transactions_are_categorized_individually(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_basic_statement(conn, usd_account)
        Categorizer(conn).run()
        twins = [
            t
            for t in TransactionRepository(conn).query().items
            if t.normalized_description == "GROCERY MART" and t.amount_minor == -9210
        ]
        assert len(twins) == 2 and all(t.category_id for t in twins)


class TestWithoutTheModel:
    """QA-04: ordinary features keep working when the AI service is unavailable."""

    @pytest.mark.parametrize("llm_factory", [lambda: None, DownLLM])
    def test_results_are_identical_with_no_model_or_a_dead_one(
        self, conn: sqlite3.Connection, usd_account: Account, llm_factory: object
    ) -> None:
        import_basic_statement(conn, usd_account)
        extra = load_transactions(conn, usd_account.id, [{"desc": "ACME BOOKSHOP"}])[0]
        report = Categorizer(conn, llm_factory()).run()  # type: ignore[operator]
        assert totals_by_category(conn) == {
            **EXPECTED_CATEGORY_TOTALS,
            "(uncategorized)": Decimal("-10.00"),
        }
        assert TransactionRepository(conn).get(extra.id).category_id is None
        assert report.remaining_uncategorized == 1

    def test_search_totals_and_manual_review_still_work_with_the_model_down(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_basic_statement(conn, usd_account)
        extra = load_transactions(conn, usd_account.id, [{"desc": "ACME BOOKSHOP"}])[0]
        c = Categorizer(conn, DownLLM())
        c.run()
        txns = TransactionRepository(conn)
        assert txns.query().total == 10
        assert [t.id for t in c.review_queue().items] == [extra.id]
        c.correct([extra.id], CategoryRepository(conn).get_by_name("Shopping").id)
        assert txns.get(extra.id).category_id is not None


class TestApprovedMappingsReduceModelUse:
    def test_second_statement_from_a_known_merchant_needs_no_model_call(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        first = load_transactions(
            conn, usd_account.id, [{"desc": "ACME BOOKSHOP", "day": "2024-01-05"}]
        )
        llm = KeywordLLM()
        c = Categorizer(conn, llm)
        c.run()
        assert len(llm.calls) == 1
        c.approve([first[0].id])  # the user confirms the AI's suggestion and remembers the merchant

        second = load_transactions(
            conn, usd_account.id, [{"desc": "ACME BOOKSHOP", "day": "2024-02-05"}]
        )
        report = c.run()
        assert len(llm.calls) == 1  # no new model call
        assert report.by_source == {"merchant_mapping": 1}
        t = TransactionRepository(conn).get(second[0].id)
        assert CategoryRepository(conn).get(t.category_id or "").name == "Shopping"

    def test_rolled_back_import_is_not_categorized(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        batch_id = load_transactions(conn, usd_account.id, [{"desc": "NETFLIX"}])[0].import_batch_id
        ImportRepository(conn).rollback(batch_id)
        assert Categorizer(conn).run().total_pending == 0


class TestOverTheLocalHttpApi:
    def test_categorization_through_the_ollama_client_and_fake_server(
        self, conn: sqlite3.Connection, usd_account: Account
    ) -> None:
        import_basic_statement(conn, usd_account)
        extra = load_transactions(
            conn,
            usd_account.id,
            [{"desc": "ACME BOOKSHOP", "amount": -123456, "day": "2024-03-09"}],
        )[0]
        with FakeOllama() as server:
            llm = OllamaClient(server.url, "llama3.2:3b", timeout_s=5)
            report = Categorizer(conn, llm).run()
            sent = server.chat_requests()
        assert report.by_source["llm"] == 1
        cat = CategoryRepository(conn).get(
            TransactionRepository(conn).get(extra.id).category_id or ""
        )
        assert cat.name == "Shopping"
        # only leftovers (not the nine rule-categorized rows) went to the model, minimally
        assert len(sent) == 1
        user = sent[0]["messages"][1]["content"]
        assert json.loads(user.split("\n")[-1]) == [{"i": 0, "desc": "ACME BOOKSHOP", "dir": "out"}]
        for private in ("123456", "1234.56", "2024", extra.id, usd_account.id):
            assert private not in json.dumps(sent)

    def test_selftest_passes_with_a_well_behaved_server_and_fails_with_a_bad_one(
        self, conn: sqlite3.Connection
    ) -> None:
        names = [c.name for c in CategoryRepository(conn).list()]
        with FakeOllama() as good:
            result = run_selftest(OllamaClient(good.url, "llama3.2:3b", timeout_s=5), names)
        assert result.reachable and result.usable and result.answered == result.total == 5
        with FakeOllama(chat=lambda p: FakeOllama.wrap({"results": "nope"})) as bad:
            result = run_selftest(OllamaClient(bad.url, "llama3.2:3b", timeout_s=5), names)
        assert result.reachable and not result.usable
        down = run_selftest(OllamaClient("http://127.0.0.1:1", "m", timeout_s=2), names)
        assert not down.reachable

    def test_check_model_script_core_check_works_without_a_model(self) -> None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "check_model.py"
        spec = importlib.util.spec_from_file_location("check_model", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.core_features_without_model() is True


def test_accounts_are_untouched_by_categorization(
    conn: sqlite3.Connection, usd_account: Account
) -> None:
    import_basic_statement(conn, usd_account)
    Categorizer(conn).run()
    assert AccountRepository(conn).get(usd_account.id) == usd_account
    assert conn.execute("SELECT COUNT(*) FROM anomaly_alerts").fetchone()[0] == 0
