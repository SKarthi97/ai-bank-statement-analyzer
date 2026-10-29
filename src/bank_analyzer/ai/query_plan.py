"""The assistant's vocabulary: a closed catalogue of query plans.

A plan is the *only* thing that can come out of the planner (rules or model). It is plain data
validated by pydantic with ``extra="forbid"``: unknown plan names, unknown fields, out-of-range
numbers and malformed dates are all rejected. A plan contains no SQL, no code and no file paths;
the executor maps each plan to a fixed, already-tested analytics function.

Periods are structured values resolved by Python against today's date, never by a model.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.domain.periods import DateRange

MAX_TEXT = 60


class PlanRejected(BankAnalyzerError):
    """A proposed plan is not in the catalogue or has invalid parameters."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PeriodSpec(_Strict):
    type: Literal["this_month", "last_month", "last_n_months", "year", "custom"]
    n: int | None = Field(default=None, ge=1, le=24)
    year: int | None = Field(default=None, ge=1990, le=2200)
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def _check(self) -> PeriodSpec:
        if self.type == "last_n_months" and self.n is None:
            raise ValueError("last_n_months needs n")
        if self.type == "year" and self.year is None:
            raise ValueError("year needs a year")
        if self.type == "custom":
            if self.start is None or self.end is None:
                raise ValueError("custom needs start and end")
            if self.start > self.end:
                raise ValueError("start is after end")
        return self

    def resolve(self, today: date) -> DateRange:
        if self.type == "this_month":
            return DateRange.month(f"{today.year:04d}-{today.month:02d}")
        if self.type == "last_month":
            return DateRange.last_n_months(today, 1)
        if self.type == "last_n_months":
            assert self.n is not None
            return DateRange.last_n_months(today, self.n)
        if self.type == "year":
            assert self.year is not None
            return DateRange.year(self.year)
        assert self.start is not None and self.end is not None
        return DateRange(self.start, self.end)


LAST_MONTH = PeriodSpec(type="last_month")
THIS_MONTH = PeriodSpec(type="this_month")

_Currency = Annotated[str | None, Field(default=None, pattern=r"^[A-Z]{3}$")]
_Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]


class CategorySpendPlan(_Strict):
    plan: Literal["category_spend"] = "category_spend"
    category: _Text
    period: PeriodSpec
    currency: _Currency = None


class SpendByCategoryPlan(_Strict):
    plan: Literal["spend_by_category"] = "spend_by_category"
    period: PeriodSpec
    currency: _Currency = None


class ComparePeriodsPlan(_Strict):
    plan: Literal["compare_periods"] = "compare_periods"
    period_a: PeriodSpec  # the earlier / baseline period
    period_b: PeriodSpec
    metric: Literal["expenses", "income", "net", "by_category"] = "expenses"
    currency: _Currency = None


class MonthlyTrendPlan(_Strict):
    plan: Literal["monthly_trend"] = "monthly_trend"
    months: int = Field(default=6, ge=1, le=24)
    metric: Literal["expenses", "income", "net"] = "expenses"
    currency: _Currency = None


class TopTransactionsPlan(_Strict):
    plan: Literal["top_transactions"] = "top_transactions"
    n: int = Field(default=5, ge=1, le=50)
    direction: Literal["out", "in"] = "out"
    period: PeriodSpec
    currency: _Currency = None


class NeedsCategorizationPlan(_Strict):
    plan: Literal["needs_categorization"] = "needs_categorization"
    limit: int = Field(default=20, ge=1, le=200)


class DuplicateCandidatesPlan(_Strict):
    plan: Literal["duplicate_candidates"] = "duplicate_candidates"
    period: PeriodSpec | None = None


class RecurringChangesPlan(_Strict):
    plan: Literal["recurring_changes"] = "recurring_changes"
    min_change_pct: int = Field(default=10, ge=1, le=500)


class ExpenseChangeDriversPlan(_Strict):
    plan: Literal["expense_change_drivers"] = "expense_change_drivers"
    period_a: PeriodSpec
    period_b: PeriodSpec
    currency: _Currency = None


class SpendingSummaryPlan(_Strict):
    plan: Literal["spending_summary"] = "spending_summary"
    period: PeriodSpec
    currency: _Currency = None


class MerchantSummaryPlan(_Strict):
    plan: Literal["merchant_summary"] = "merchant_summary"
    merchant: _Text  # matched as plain text, never as a pattern
    period: PeriodSpec
    currency: _Currency = None


QueryPlan = Annotated[
    CategorySpendPlan
    | SpendByCategoryPlan
    | ComparePeriodsPlan
    | MonthlyTrendPlan
    | TopTransactionsPlan
    | NeedsCategorizationPlan
    | DuplicateCandidatesPlan
    | RecurringChangesPlan
    | ExpenseChangeDriversPlan
    | SpendingSummaryPlan
    | MerchantSummaryPlan,
    Field(discriminator="plan"),
]

PLAN_MODELS: dict[str, type[_Strict]] = {
    m.model_fields["plan"].default: m
    for m in (
        CategorySpendPlan,
        SpendByCategoryPlan,
        ComparePeriodsPlan,
        MonthlyTrendPlan,
        TopTransactionsPlan,
        NeedsCategorizationPlan,
        DuplicateCandidatesPlan,
        RecurringChangesPlan,
        ExpenseChangeDriversPlan,
        SpendingSummaryPlan,
        MerchantSummaryPlan,
    )
}


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: QueryPlan


def parse_plan(raw: object) -> QueryPlan:
    """Validate untrusted data (a dict from a model, say) into a plan or raise PlanRejected."""
    if not isinstance(raw, dict):
        raise PlanRejected("The plan must be a JSON object.")
    if raw.get("plan") not in PLAN_MODELS:
        raise PlanRejected("The plan is not one of the supported questions.")
    try:
        return _Envelope.model_validate({"plan": raw}).plan
    except ValidationError as exc:
        raise PlanRejected("The plan has invalid parameters.") from exc


def model_plan_schema() -> dict[str, object]:
    """JSON schema handed to the model: a catalogue name plus free parameters.

    Deliberately simple (servers handle small schemas best). It narrows the reply but is not
    trusted: ``parse_plan`` re-validates every field."""
    return {
        "type": "object",
        "properties": {
            "plan": {"type": "string", "enum": [*PLAN_MODELS, "unsupported"]},
            "params": {"type": "object"},
        },
        "required": ["plan"],
        "additionalProperties": False,
    }


CATALOGUE_HELP: tuple[str, ...] = (
    "How much did I spend on <category> last month?",
    "Show spending by category this month.",
    "Compare this month with last month.",
    "Show my monthly spending trend for the last 6 months.",
    "What are my 5 largest expenses last month?",
    "Which transactions need categorization?",
    "Are there duplicate charges?",
    "Which subscriptions increased in price?",
    "Why did my expenses change compared with last month?",
    "Give me a summary of last month.",
    "How much did I spend at <merchant> in 2025?",
)
