"""The plan catalogue accepts only well-formed, in-range plans and resolves periods exactly."""

from __future__ import annotations

import contextlib
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bank_analyzer.ai.query_plan import (
    PLAN_MODELS,
    CategorySpendPlan,
    PeriodSpec,
    PlanRejected,
    model_plan_schema,
    parse_plan,
)
from bank_analyzer.domain.periods import DateRange

TODAY = date(2024, 3, 20)
LAST = {"type": "last_month"}


class TestParsePlan:
    def test_valid_plan(self) -> None:
        plan = parse_plan({"plan": "category_spend", "category": "Groceries", "period": LAST})
        assert isinstance(plan, CategorySpendPlan)
        assert plan.category == "Groceries"

    def test_defaults_apply(self) -> None:
        plan = parse_plan({"plan": "top_transactions", "period": LAST})
        assert (plan.n, plan.direction) == (5, "out")  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "raw",
        [
            "category_spend",  # not an object
            None,
            [],
            {},  # no plan name
            {"plan": "drop_table", "table": "transactions"},  # unknown plan
            {"plan": "sql", "query": "SELECT * FROM transactions"},
            {
                "plan": "category_spend",
                "category": "Groceries",
                "period": LAST,
                "sql": "x",
            },  # extra field
            {"plan": "category_spend", "period": LAST},  # missing required field
            {"plan": "category_spend", "category": "", "period": LAST},
            {"plan": "category_spend", "category": "x" * 61, "period": LAST},
            {"plan": "category_spend", "category": "Groceries", "period": LAST, "currency": "usd"},
            {"plan": "category_spend", "category": "Groceries", "period": LAST, "currency": "US"},
            {"plan": "top_transactions", "n": 0, "period": LAST},
            {"plan": "top_transactions", "n": 51, "period": LAST},
            {"plan": "top_transactions", "n": 5.5, "period": LAST},
            {"plan": "top_transactions", "direction": "sideways", "period": LAST},
            {"plan": "monthly_trend", "months": 25},
            {"plan": "needs_categorization", "limit": 201},
            {"plan": "recurring_changes", "min_change_pct": 501},
            {"plan": "recurring_changes", "min_change_pct": 0},
            {"plan": "spending_summary", "period": {"type": "tomorrow"}},
            {"plan": "spending_summary", "period": {"type": "last_n_months"}},  # needs n
            {"plan": "spending_summary", "period": {"type": "last_n_months", "n": 25}},
            {"plan": "spending_summary", "period": {"type": "year"}},
            {"plan": "spending_summary", "period": {"type": "custom", "start": "2024-01-01"}},
            {
                "plan": "spending_summary",
                "period": {"type": "custom", "start": "2024-03-01", "end": "2024-01-01"},
            },
            {"plan": "spending_summary", "period": {"type": "custom", "start": "x", "end": "y"}},
        ],
    )
    def test_rejects(self, raw: object) -> None:
        with pytest.raises(PlanRejected):
            parse_plan(raw)

    def test_text_parameters_are_plain_data(self) -> None:
        # a hostile string is accepted as text (the executor never builds SQL or a pattern from it)
        plan = parse_plan(
            {
                "plan": "merchant_summary",
                "merchant": "'; DROP TABLE transactions;--",
                "period": LAST,
            }
        )
        assert plan.merchant == "'; DROP TABLE transactions;--"  # type: ignore[union-attr]

    def test_every_catalogue_entry_is_reachable_by_name(self) -> None:
        assert set(PLAN_MODELS) == {
            "category_spend",
            "spend_by_category",
            "compare_periods",
            "monthly_trend",
            "top_transactions",
            "needs_categorization",
            "duplicate_candidates",
            "recurring_changes",
            "expense_change_drivers",
            "spending_summary",
            "merchant_summary",
        }

    def test_model_schema_lists_the_catalogue(self) -> None:
        enum = model_plan_schema()["properties"]["plan"]["enum"]  # type: ignore[index]
        assert set(enum) == {*PLAN_MODELS, "unsupported"}

    @given(
        st.dictionaries(
            st.text(max_size=12),
            st.one_of(st.none(), st.integers(), st.text(max_size=20), st.booleans()),
            max_size=6,
        )
    )
    def test_arbitrary_input_is_a_plan_or_a_rejection(self, raw: dict[str, object]) -> None:
        with contextlib.suppress(PlanRejected):
            parse_plan(raw)


class TestPeriodResolution:
    def test_this_month(self) -> None:
        assert PeriodSpec(type="this_month").resolve(TODAY) == DateRange(
            date(2024, 3, 1), date(2024, 3, 31)
        )

    def test_last_month(self) -> None:
        assert PeriodSpec(type="last_month").resolve(TODAY) == DateRange(
            date(2024, 2, 1), date(2024, 2, 29)
        )

    def test_last_month_across_new_year(self) -> None:
        assert PeriodSpec(type="last_month").resolve(date(2024, 1, 5)) == DateRange(
            date(2023, 12, 1), date(2023, 12, 31)
        )

    def test_last_n_months_are_complete_months(self) -> None:
        assert PeriodSpec(type="last_n_months", n=3).resolve(TODAY) == DateRange(
            date(2023, 12, 1), date(2024, 2, 29)
        )

    def test_year_and_custom(self) -> None:
        assert PeriodSpec(type="year", year=2023).resolve(TODAY) == DateRange.year(2023)
        spec = PeriodSpec(type="custom", start=date(2024, 1, 5), end=date(2024, 1, 9))
        assert spec.resolve(TODAY).days == 5
