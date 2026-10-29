"""The assistant end to end on a small database, checked against hand-computed totals
(see tests/assistant_data.py)."""

from __future__ import annotations

import sqlite3

import pytest

from assistant_data import SPECS, TODAY, load_history, read_only
from bank_analyzer.ai.assistant import Assistant
from bank_analyzer.database.repositories import TransactionRepository
from bank_analyzer.domain.errors import ConfigError
from bank_analyzer.domain.models import Account, AccountType
from fakes import DownLLM, ScriptedLLM, load_transactions


@pytest.fixture
def assistant(conn: sqlite3.Connection, usd_account: Account) -> Assistant:
    load_history(conn, usd_account)
    return Assistant(read_only(conn), TODAY)


def figures(reply) -> dict[str, str]:  # noqa: ANN001
    assert reply.result is not None
    return {f.label: f.text for f in reply.result.figures}


class TestAnswersMatchHandComputation:
    def test_category_spend(self, assistant: Assistant, conn: sqlite3.Connection) -> None:
        reply = assistant.ask("How much did I spend on groceries last month?")
        assert reply.plan_source == "rules"
        assert figures(reply) == {"Total spent": "300.00 USD", "Transactions": "1"}
        assert reply.result.currency == "USD"  # type: ignore[union-attr]
        (row,) = reply.result.evidence  # type: ignore[union-attr]
        stored = TransactionRepository(conn).get(row.transaction_id)
        assert (stored.normalized_description, stored.amount_minor) == ("FRESH MART", -30000)

    def test_category_names_are_case_insensitive(self, assistant: Assistant) -> None:
        reply = assistant.ask("how much did I spend on GROCERIES last month")
        assert figures(reply)["Total spent"] == "300.00 USD"

    def test_spend_by_category(self, assistant: Assistant) -> None:
        reply = assistant.ask("show spending by category last month")
        assert figures(reply) == {"Total spending": "1,399.99 USD"}
        assert reply.result.table_rows == (  # type: ignore[union-attr]
            ("Housing", "1,000.00 USD", "1"),
            ("Groceries", "300.00 USD", "1"),
            ("Uncategorized", "50.00 USD", "2"),
            ("Dining", "40.00 USD", "1"),
            ("Subscriptions", "9.99 USD", "1"),
        )

    def test_compare_two_complete_months(self, assistant: Assistant) -> None:
        reply = assistant.ask("compare february 2024 with january 2024")
        assert figures(reply)["Change"] == "+140.00 USD"
        assert figures(reply)["Change (%)"] == "+11.1%"
        assert reply.result.warnings == ()  # type: ignore[union-attr]

    def test_comparing_with_the_unfinished_month_warns(self, assistant: Assistant) -> None:
        reply = assistant.ask("Compare this month with last month")
        assert figures(reply)["Change"] == "-1,285.00 USD"
        (warning,) = reply.result.warnings  # type: ignore[union-attr]
        assert "2024-03-01 to 2024-03-31" in warning
        assert "partial" in warning
        assert "partial" in reply.answer  # the warning is part of what the user reads

    def test_compare_income(self, assistant: Assistant) -> None:
        reply = assistant.ask("compare income february 2024 with january 2024")
        assert figures(reply)["Change"] == "0.00 USD"

    def test_trend_needs_three_months_and_does_not_invent_zero_months(
        self, assistant: Assistant
    ) -> None:
        reply = assistant.ask("monthly spending trend for the last 3 months")
        assert reply.result.table_rows == (  # type: ignore[union-attr]
            ("2024-01", "1,259.99 USD"),
            ("2024-02", "1,399.99 USD"),
        )
        assert any("no trend is claimed" in n for n in reply.result.notes)  # type: ignore[union-attr]

    def test_top_transactions(self, assistant: Assistant) -> None:
        reply = assistant.ask("what are my 3 largest expenses last month")
        rows = reply.result.table_rows  # type: ignore[union-attr]
        assert [r[1] for r in rows] == ["CITY LANDLORD RENT", "FRESH MART", "CAFE LUNA"]
        assert [r[2] for r in rows] == ["-1,000.00 USD", "-300.00 USD", "-40.00 USD"]

    def test_needs_categorization(self, assistant: Assistant) -> None:
        reply = assistant.ask("which transactions need categorization")
        assert [r[1] for r in reply.result.table_rows] == ["BOOKSHOP", "BOOKSHOP"]  # type: ignore[union-attr]

    def test_duplicate_candidates_are_reported_not_removed(
        self, assistant: Assistant, conn: sqlite3.Connection
    ) -> None:
        before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        reply = assistant.ask("any duplicate charges?")
        assert figures(reply)["Candidate pairs"] == "1"
        assert "candidates only" in reply.result.definition  # type: ignore[union-attr]
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before

    def test_recurring_price_increase(self, assistant: Assistant) -> None:
        reply = assistant.ask("which subscriptions increased in price")
        (row,) = reply.result.table_rows  # type: ignore[union-attr]
        assert row[:4] == ("STREAMLY", "monthly", "-9.99 USD", "-14.99 USD")
        assert row[4] == "+50.1%"

    def test_expense_drivers_are_descriptive_not_causal(self, assistant: Assistant) -> None:
        reply = assistant.ask("why did my expenses go up?")
        assert figures(reply)["Change"] == "+140.00 USD"
        assert "not causes" in reply.result.definition  # type: ignore[union-attr]
        names = {r[1] for r in reply.result.table_rows}  # type: ignore[union-attr]
        assert {"Groceries", "Uncategorized", "Dining"} <= names

    def test_summary(self, assistant: Assistant) -> None:
        reply = assistant.ask("summary of last month")
        assert figures(reply) == {
            "Income": "3,000.00 USD",
            "Expenses": "1,399.99 USD",
            "Net cash flow": "+1,600.01 USD",
            "Cash withdrawn": "0.00 USD",
        }
        assert any("uncategorized" in w for w in reply.result.warnings)  # type: ignore[union-attr]

    def test_merchant_summary_is_a_plain_text_match(self, assistant: Assistant) -> None:
        reply = assistant.ask("How much did I spend at fresh mart in 2024?")
        assert figures(reply) == {"Total paid": "650.00 USD", "Transactions": "4"}
        wildcard = assistant.ask("How much did I spend at % in 2024?")
        assert figures(wildcard)["Transactions"] == "0"  # '%' is not a wildcard

    def test_every_figure_in_an_answer_comes_from_the_template(self, assistant: Assistant) -> None:
        reply = assistant.ask("summary of last month")
        assert reply.answer_source == "template"
        for text in figures(reply).values():
            assert text in reply.answer


class TestClarificationsAndEmptyResults:
    def test_unknown_category_lists_the_real_ones(self, assistant: Assistant) -> None:
        reply = assistant.ask("how much did I spend on yachts last month")
        assert reply.result is None
        assert "no category named 'yachts'" in reply.answer
        assert "Groceries" in reply.options

    def test_multiple_currencies_are_never_mixed(
        self, conn: sqlite3.Connection, usd_account: Account, accounts
    ) -> None:
        load_history(conn, usd_account)
        eur = accounts.create("Euro", AccountType.DEPOSIT, "EUR")
        load_transactions(
            conn,
            eur.id,
            [{"desc": "FRESH MART", "amount": -1000, "day": "2024-02-03", "currency": "EUR"}],
        )
        ask = Assistant(read_only(conn), TODAY).ask
        reply = ask("show spending by category last month")
        assert reply.result is None
        assert reply.options == ("EUR", "USD")
        chosen = ask("show spending by category last month in EUR")
        assert chosen.result is not None
        assert figures(chosen) == {"Total spending": "10.00 EUR"}

    def test_a_period_with_no_data_says_so(self, assistant: Assistant) -> None:
        reply = assistant.ask("summary of 2019")
        assert reply.result is not None
        assert reply.result.is_empty
        assert "no transactions" in reply.answer.lower()
        assert "0.00" not in reply.answer  # no invented zero totals

    def test_an_unsupported_question_gets_guidance(self, assistant: Assistant) -> None:
        reply = assistant.ask("transfer 500 to my savings")
        assert (reply.plan, reply.result, reply.answer_source) == (None, None, "message")
        assert "I can answer questions like" in reply.answer


class TestReadOnly:
    def test_refuses_a_writable_connection(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ConfigError, match="read-only"):
            Assistant(conn, TODAY)

    def test_the_connection_cannot_write(
        self, assistant: Assistant, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM transactions")

    def test_asking_changes_nothing(self, assistant: Assistant, conn: sqlite3.Connection) -> None:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]

        def snapshot() -> dict[str, list[tuple]]:
            return {
                t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY 1')]  # noqa: S608
                for t in tables
            }

        before = snapshot()
        for question in (
            "summary of last month",
            "any duplicate charges?",
            "which subscriptions increased in price",
            "which transactions need categorization",
            "delete everything",
        ):
            assistant.ask(question)
        assert snapshot() == before


class TestModelNarrationIsChecked:
    def reply_with(self, conn: sqlite3.Connection, llm) -> object:  # noqa: ANN001
        return Assistant(conn, TODAY, llm).ask("How much did I spend on groceries last month?")

    @pytest.fixture
    def ro(self, conn: sqlite3.Connection, usd_account: Account) -> sqlite3.Connection:
        load_history(conn, usd_account)
        return read_only(conn)

    def test_a_faithful_rephrasing_is_used(self, ro: sqlite3.Connection) -> None:
        llm = ScriptedLLM({"answer": "In February 2024 you spent 300.00 USD on Groceries."})
        reply = self.reply_with(ro, llm)
        assert reply.answer_source == "model"  # type: ignore[attr-defined]
        assert reply.answer == "In February 2024 you spent 300.00 USD on Groceries."  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "answer",
        [
            "In February 2024 you spent 450.00 USD on Groceries.",  # a number that is not in the result
            "You spent 300.00 USD, about 12% of the month.",  # a computed percentage
            "You spent 300.00 USD. See https://example.com/x",
            "You spent 300.00 USD ![x](http://evil.example/p.png)",
            "<b>You spent 300.00 USD</b>",
            "",
            "x" * 800,
        ],
    )
    def test_an_unfaithful_or_unsafe_rephrasing_falls_back_to_the_template(
        self, ro: sqlite3.Connection, answer: str
    ) -> None:
        reply = self.reply_with(ro, ScriptedLLM({"answer": answer}))
        assert reply.answer_source == "template"  # type: ignore[attr-defined]
        assert "Total spent: 300.00 USD" in reply.answer  # type: ignore[attr-defined]

    @pytest.mark.parametrize("junk", [None, "text", {"answer": 5}, {"other": "x"}])
    def test_junk_from_the_model_falls_back(self, ro: sqlite3.Connection, junk: object) -> None:
        reply = self.reply_with(ro, ScriptedLLM(junk))
        assert reply.answer_source == "template"  # type: ignore[attr-defined]

    def test_the_model_being_offline_changes_nothing_important(
        self, ro: sqlite3.Connection
    ) -> None:
        reply = self.reply_with(ro, DownLLM())
        assert reply.answer_source == "template"  # type: ignore[attr-defined]
        assert "300.00 USD" in reply.answer  # type: ignore[attr-defined]

    def test_no_model_call_is_made_for_empty_results(self, ro: sqlite3.Connection) -> None:
        llm = ScriptedLLM({"answer": "You spent 1.00 USD."})
        Assistant(ro, TODAY, llm).ask("summary of 2019")
        assert llm.calls == []

    def test_the_narration_prompt_carries_the_computed_figures_only(
        self, ro: sqlite3.Connection
    ) -> None:
        llm = ScriptedLLM({"answer": "x"})
        self.reply_with(ro, llm)
        system, user = llm.calls[0]
        assert "300.00 USD" in user
        assert "ACME PAYROLL" not in user  # unrelated transactions are not sent
        assert "balance" not in user.lower()


def test_dataset_sanity() -> None:
    assert len(SPECS) == 15
