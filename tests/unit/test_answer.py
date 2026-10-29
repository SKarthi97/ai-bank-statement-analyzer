"""Narration checking: numbers must come from the result; no links or markup; markdown escaping."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bank_analyzer.ai.answer import (
    escape_markdown,
    numbers_in,
    template_answer,
    validate_narration,
)
from bank_analyzer.ai.query_executor import AnalysisResult, EvidenceRow, Figure
from bank_analyzer.domain.periods import DateRange

RESULT = AnalysisResult(
    plan="spending_summary",
    title="Summary",
    definition="Income and expenses.",
    periods=(DateRange(date(2024, 2, 1), date(2024, 2, 29)),),
    currency="USD",
    figures=(Figure("Income", "3,000.00 USD"), Figure("Expenses", "1,399.99 USD")),
    table_columns=("Kind", "Name", "Amount"),
    table_rows=(("Category", "Housing", "1,000.00 USD"),),
    evidence=(EvidenceRow("id-1", date(2024, 2, 6), "CITY LANDLORD RENT", "-1,000.00 USD"),),
    transaction_count=9,
)


class TestNumbersIn:
    def test_formatting_does_not_matter(self) -> None:
        assert numbers_in("1,234.50 USD") == numbers_in("1234.5 usd") == {"1234.5"}

    def test_dates_and_counts(self) -> None:
        assert numbers_in("2024-02-01 had 9 rows") == {"2024", "2", "1", "9"} | {"2024"}

    def test_trailing_zeros_are_equal(self) -> None:
        assert numbers_in("3,000.00") == numbers_in("3000") == {"3E+3"}

    @given(st.text(max_size=80))
    def test_never_raises(self, text: str) -> None:
        numbers_in(text)


class TestValidateNarration:
    @pytest.mark.parametrize(
        "text",
        [
            "In February 2024 your income was 3,000.00 USD and expenses were 1,399.99 USD.",
            "You spent 1399.99 USD in February 2024.",
            "Housing was the largest category at 1,000.00 USD.",
            "No numbers at all, just words.",
        ],
    )
    def test_accepts_faithful_text(self, text: str) -> None:
        assert validate_narration(text, RESULT)

    @pytest.mark.parametrize(
        "text",
        [
            "You spent 1,400.00 USD.",  # rounded: not a figure in the result
            "Expenses were 46.67 USD per day.",  # derived
            "Savings were 53.3% of income.",  # derived
            "Income was 3,000.00 USD (see http://evil.example).",
            "Income was 3,000.00 USD www.evil.example",
            "![pixel](x) 3,000.00 USD",
            "[click](x) 3,000.00 USD",
            "<script>3,000.00</script>",
            "`3,000.00 USD`",
            "javascript:alert(1) 3,000.00",
            "",
            "a" * 701,
        ],
    )
    def test_rejects(self, text: str) -> None:
        assert not validate_narration(text, RESULT)


class TestStatementTextIsNotASourceOfNumbers:
    """A figure forged inside statement text must not make a narration pass the number check."""

    def test_evidence_descriptions(self) -> None:
        forged = AnalysisResult(
            "x",
            "Largest",
            "d",
            (),
            "USD",
            (Figure("Total", "10.00 USD"),),
            evidence=(EvidenceRow("i", date(2024, 2, 6), "PAY 777777.77 NOW", "-10.00 USD"),),
        )
        assert validate_narration("You paid 10.00 USD.", forged)
        assert not validate_narration("You owe 777777.77 USD.", forged)

    @pytest.mark.parametrize("column", ["Description", "Merchant", "Name", "Category"])
    def test_name_columns_in_tables(self, column: str) -> None:
        table = AnalysisResult(
            "x",
            "Rows",
            "d",
            (),
            "USD",
            (),
            table_columns=(column, "Amount"),
            table_rows=(("SHOP 424242.42", "10.00 USD"),),
        )
        assert validate_narration("The amount was 10.00 USD.", table)
        assert not validate_narration("The amount was 424242.42 USD.", table)


class TestTemplate:
    def test_states_period_currency_and_every_figure(self) -> None:
        text = template_answer(RESULT)
        assert "Period: 2024-02-01 to 2024-02-29" in text
        assert "Currency: USD" in text
        assert "Income: 3,000.00 USD" in text
        assert "Expenses: 1,399.99 USD" in text
        assert "- Category, Housing, 1,000.00 USD" in text

    def test_passes_its_own_validation(self) -> None:
        # the template is built only from the result, so it can never fail the number check
        assert numbers_in(template_answer(RESULT)) <= numbers_in(
            "\n".join(f.text for f in RESULT.figures) + "\n2024-02-01 2024-02-29 1,000.00 USD"
        )

    def test_long_tables_are_truncated_with_a_pointer(self) -> None:
        rows = tuple((f"cat{i}", f"{i}.00 USD") for i in range(8))
        long = AnalysisResult(
            "x", "T", "d", (), None, (), table_columns=("A", "B"), table_rows=rows
        )
        text = template_answer(long)
        assert text.count("\n- ") == 5
        assert "and 3 more" in text

    def test_transaction_ids_are_left_out_of_the_text(self) -> None:
        result = AnalysisResult(
            "x",
            "T",
            "d",
            (),
            None,
            (),
            table_columns=("Date", "Description", "Amount", "Transaction"),
            table_rows=(("2024-02-06", "RENT", "-1,000.00 USD", "9f1c-secret-id"),),
        )
        assert "secret-id" not in template_answer(result)

    def test_empty_result_says_so(self) -> None:
        empty = AnalysisResult("x", "T", "d", (), None, ())
        assert "No transactions matched" in template_answer(empty)


class TestEscapeMarkdown:
    @pytest.mark.parametrize(
        "text",
        [
            "![x](http://evil.example/p.png)",
            "[link](http://evil.example)",
            "<img src=x onerror=alert(1)>",
            "`code` **bold** _it_ # head | table",
            "$x$",
        ],
    )
    def test_every_special_character_is_escaped(self, text: str) -> None:
        escaped = escape_markdown(text)
        for i, ch in enumerate(escaped):
            if ch in "\\`*_{}[]()#+-.!|<>~$":
                # either it is the escaping backslash itself or it is preceded by one
                assert ch == "\\" or escaped[i - 1] == "\\", escaped

    def test_plain_text_is_unchanged(self) -> None:
        assert escape_markdown("FRESH MART 12") == "FRESH MART 12"
