"""Runs a validated query plan against the database and returns an evidence-carrying result.

The executor is the assistant's only data path. It receives a *read-only* connection (see
``connect_readonly``), so even a defect here cannot change data. Each plan maps to a fixed
analytics call; the question text and any model output never reach SQL or code. Every figure is
computed by the Phase 5 analytics in exact integer arithmetic.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from bank_analyzer.ai.query_plan import (
    CategorySpendPlan,
    ComparePeriodsPlan,
    DuplicateCandidatesPlan,
    ExpenseChangeDriversPlan,
    MerchantSummaryPlan,
    MonthlyTrendPlan,
    NeedsCategorizationPlan,
    QueryPlan,
    RecurringChangesPlan,
    SpendByCategoryPlan,
    SpendingSummaryPlan,
    TopTransactionsPlan,
)
from bank_analyzer.database.repositories import (
    CategoryRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.domain.models import CanonicalTransaction
from bank_analyzer.domain.money import Money
from bank_analyzer.domain.periods import DateRange
from bank_analyzer.services.analytics import Analyzer
from bank_analyzer.services.duplicates import find_duplicate_pairs
from bank_analyzer.services.recurring import amount_changes, detect_recurring

EVIDENCE_ROWS_MAX = 20  # transactions listed as evidence (and the most a model ever sees)
_PCT = Decimal("0.1")


class NeedsClarification(BankAnalyzerError):
    """The question cannot be answered as asked; ``options`` says what to choose from."""

    def __init__(self, message: str, options: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.options = tuple(options)


@dataclass(frozen=True, slots=True)
class Figure:
    label: str
    text: str  # display text, e.g. "1,234.50 USD" or "12.5%"


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    transaction_id: str
    txn_date: date
    description: str
    amount_text: str


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Everything an answer is allowed to say, with the definition and the supporting rows."""

    plan: str
    title: str
    definition: str
    periods: tuple[DateRange, ...]
    currency: str | None
    figures: tuple[Figure, ...]
    table_columns: tuple[str, ...] = ()
    table_rows: tuple[tuple[str, ...], ...] = ()
    evidence: tuple[EvidenceRow, ...] = ()
    transaction_count: int = 0
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.transaction_count == 0


def _pct_text(delta: int, base: int) -> str:
    if base == 0:
        return "n/a (the baseline is zero)"
    with localcontext() as ctx:
        ctx.prec = 28
        pct = (Decimal(delta) * 100 / Decimal(abs(base))).quantize(_PCT, rounding=ROUND_HALF_EVEN)
    return f"{pct:+}%"


def _signed(money: Money) -> str:
    text = money.format()
    return text if money.minor <= 0 else f"+{text}"


class QueryExecutor:
    def __init__(self, conn: sqlite3.Connection, today: date) -> None:
        self._conn = conn
        self._today = today
        self._txns = TransactionRepository(conn)
        self._analyzer = Analyzer.from_db(conn)
        self._categories = {c.name.casefold(): c for c in CategoryRepository(conn).list()}

    # -- helpers ----------------------------------------------------------------------------

    def _currency(self, requested: str | None, periods: Sequence[DateRange]) -> str | None:
        if requested is not None:
            return requested
        present: set[str] = set()
        for period in periods:
            present.update(self._analyzer.currencies(period))
        if len(present) > 1:
            raise NeedsClarification(
                "There are transactions in more than one currency for this period. "
                "Totals are never mixed; ask again naming one currency.",
                sorted(present),
            )
        return next(iter(present), None)

    def _evidence(self, ids: Sequence[str]) -> tuple[EvidenceRow, ...]:
        rows = []
        for txn_id in ids[:EVIDENCE_ROWS_MAX]:
            txn = self._txns.get(txn_id)
            rows.append(_row(txn))
        return tuple(rows)

    def _empty(self, plan: str, title: str, periods: Sequence[DateRange]) -> AnalysisResult:
        return AnalysisResult(
            plan,
            title,
            "No transactions matched.",
            tuple(periods),
            None,
            (),
            notes=("There are no transactions in this period, so there is nothing to report.",),
        )

    # -- dispatch ---------------------------------------------------------------------------

    def execute(self, plan: QueryPlan) -> AnalysisResult:
        result = self._dispatch(plan)
        unfinished = [p for p in result.periods if p.end > self._today]
        if not unfinished:
            return result
        warning = (
            "Part of this period is still ahead of today ("
            + ", ".join(p.label() for p in unfinished)
            + f", today is {self._today.isoformat()}), so its totals are partial and a "
            "comparison with a complete period can look like a large drop."
        )
        return replace(result, warnings=(*result.warnings, warning))

    def _dispatch(self, plan: QueryPlan) -> AnalysisResult:
        if isinstance(plan, CategorySpendPlan):
            return self._category_spend(plan)
        if isinstance(plan, SpendByCategoryPlan):
            return self._spend_by_category(plan)
        if isinstance(plan, ComparePeriodsPlan):
            return self._compare(plan)
        if isinstance(plan, MonthlyTrendPlan):
            return self._trend(plan)
        if isinstance(plan, TopTransactionsPlan):
            return self._top(plan)
        if isinstance(plan, NeedsCategorizationPlan):
            return self._needs_categorization(plan)
        if isinstance(plan, DuplicateCandidatesPlan):
            return self._duplicates(plan)
        if isinstance(plan, RecurringChangesPlan):
            return self._recurring(plan)
        if isinstance(plan, ExpenseChangeDriversPlan):
            return self._drivers(plan)
        if isinstance(plan, SpendingSummaryPlan):
            return self._summary(plan)
        return self._merchant(plan)

    # -- plans ------------------------------------------------------------------------------

    def _category_spend(self, plan: CategorySpendPlan) -> AnalysisResult:
        period = plan.period.resolve(self._today)
        category = self._categories.get(plan.category.casefold())
        if category is None:
            raise NeedsClarification(
                f"There is no category named '{plan.category}'.",
                sorted(c.name for c in self._categories.values()),
            )
        currency = self._currency(plan.currency, [period])
        title = f"Spending on {category.name}"
        if currency is None:
            return self._empty("category_spend", title, [period])
        rows = [
            r
            for r in self._analyzer.category_spend(period, currency)
            if r.category_id == category.id
        ]
        amount = rows[0].amount if rows else Money.zero(currency)
        ids = rows[0].transaction_ids if rows else ()
        return AnalysisResult(
            "category_spend",
            title,
            "Money spent in this category, net of refunds linked to its purchases; matched "
            "transfers are excluded.",
            (period,),
            currency,
            (Figure("Total spent", amount.format()), Figure("Transactions", str(len(ids)))),
            evidence=self._evidence(ids),
            transaction_count=len(ids),
            notes=() if ids else ("No transactions in this category in the period.",),
        )

    def _spend_by_category(self, plan: SpendByCategoryPlan) -> AnalysisResult:
        period = plan.period.resolve(self._today)
        currency = self._currency(plan.currency, [period])
        if currency is None:
            return self._empty("spend_by_category", "Spending by category", [period])
        rows = self._analyzer.category_spend(period, currency)
        total = sum((r.amount.minor for r in rows), 0)
        ids = tuple(i for r in rows for i in r.transaction_ids)
        return AnalysisResult(
            "spend_by_category",
            "Spending by category",
            "Spending per category, net of linked refunds; uncategorized money out is shown "
            "as 'Uncategorized'. Matched transfers are excluded.",
            (period,),
            currency,
            (Figure("Total spending", Money(total, currency).format()),),
            ("Category", "Amount", "Transactions"),
            tuple((r.name, r.amount.format(), str(r.count)) for r in rows),
            self._evidence(ids),
            len(ids),
        )

    def _metric(self, period: DateRange, currency: str, metric: str) -> Money:
        s = self._analyzer.summary(period, currency)
        return {"expenses": s.expenses, "income": s.income, "net": s.net}[metric]

    def _compare(self, plan: ComparePeriodsPlan) -> AnalysisResult:
        a, b = plan.period_a.resolve(self._today), plan.period_b.resolve(self._today)
        currency = self._currency(plan.currency, [a, b])
        title = f"Compare {plan.metric.replace('_', ' ')}"
        if currency is None:
            return self._empty("compare_periods", title, [a, b])
        if plan.metric == "by_category":
            before = {r.name: r for r in self._analyzer.category_spend(a, currency)}
            after = {r.name: r for r in self._analyzer.category_spend(b, currency)}
            rows = []
            for name in sorted(set(before) | set(after)):
                x = before[name].amount if name in before else Money.zero(currency)
                y = after[name].amount if name in after else Money.zero(currency)
                rows.append((name, x, y, y - x))
            rows.sort(key=lambda r: (-abs(r[3].minor), r[0]))
            ids = tuple(
                i for src in (before, after) for r in src.values() for i in r.transaction_ids
            )
            return AnalysisResult(
                "compare_periods",
                title,
                "Spending per category in each period (net of linked refunds).",
                (a, b),
                currency,
                (),
                ("Category", f"{a.label()}", f"{b.label()}", "Change"),
                tuple((n, x.format(), y.format(), _signed(d)) for n, x, y, d in rows),
                self._evidence(ids),
                len(ids),
            )
        x, y = self._metric(a, currency, plan.metric), self._metric(b, currency, plan.metric)
        delta = y - x
        count = self._analyzer.summary(a, currency).transaction_count + (
            self._analyzer.summary(b, currency).transaction_count
        )
        notes = []
        if self._analyzer.summary(a, currency).transaction_count == 0:
            notes.append(
                "The first period has no transactions, so the comparison is not meaningful."
            )
        if self._analyzer.summary(b, currency).transaction_count == 0:
            notes.append(
                "The second period has no transactions, so the comparison is not meaningful."
            )
        return AnalysisResult(
            "compare_periods",
            title,
            f"{plan.metric.capitalize()} as defined in the analytics; transfers between your own "
            "accounts are excluded.",
            (a, b),
            currency,
            (
                Figure(f"First period ({a.label()})", x.format()),
                Figure(f"Second period ({b.label()})", y.format()),
                Figure("Change", _signed(delta)),
                Figure("Change (%)", _pct_text(delta.minor, x.minor)),
            ),
            transaction_count=count,
            notes=tuple(notes),
        )

    def _trend(self, plan: MonthlyTrendPlan) -> AnalysisResult:
        period = DateRange.last_n_months(self._today, plan.months)
        currency = self._currency(plan.currency, [period])
        title = f"Monthly {plan.metric}"
        if currency is None:
            return self._empty("monthly_trend", title, [period])
        flows = [f for f in self._analyzer.monthly(period, currency) if f.has_data]
        series = [(f.month, getattr(f.summary, plan.metric)) for f in flows]
        count = sum(f.summary.transaction_count for f in flows)
        notes = ["Months with no transactions are left out rather than shown as zero."]
        if len(series) < 3:
            notes.append("Fewer than 3 months have data, so no trend is claimed.")
        return AnalysisResult(
            "monthly_trend",
            title,
            f"{plan.metric.capitalize()} per calendar month (as defined in the analytics).",
            (period,),
            currency,
            (),
            ("Month", plan.metric.capitalize()),
            tuple((m, v.format()) for m, v in series),
            (),
            count,
            notes=tuple(notes),
        )

    def _top(self, plan: TopTransactionsPlan) -> AnalysisResult:
        period = plan.period.resolve(self._today)
        currency = self._currency(plan.currency, [period])
        title = f"Largest {'expenses' if plan.direction == 'out' else 'deposits'}"
        if currency is None:
            return self._empty("top_transactions", title, [period])
        rows = self._analyzer.largest_transactions(
            period, currency, n=plan.n, direction=plan.direction
        )
        return AnalysisResult(
            "top_transactions",
            title,
            "The biggest transactions by amount, matched transfers excluded.",
            (period,),
            currency,
            (),
            ("Date", "Description", "Amount", "Transaction"),
            tuple(
                (t.txn_date.isoformat(), t.normalized_description, t.amount.format(), t.id)
                for t in rows
            ),
            tuple(_row(t) for t in rows[:EVIDENCE_ROWS_MAX]),
            len(rows),
        )

    def _needs_categorization(self, plan: NeedsCategorizationPlan) -> AnalysisResult:
        rows = self._txns.list_pending_categorization(limit=plan.limit)
        return AnalysisResult(
            "needs_categorization",
            "Transactions without a category",
            "Live transactions with no category that you have not approved or rejected.",
            (),
            None,
            (Figure("Shown", str(len(rows))),),
            ("Date", "Description", "Amount", "Transaction"),
            tuple(
                (t.txn_date.isoformat(), t.normalized_description, t.amount.format(), t.id)
                for t in rows
            ),
            tuple(_row(t) for t in rows[:EVIDENCE_ROWS_MAX]),
            len(rows),
            notes=(f"At most {plan.limit} are listed.",),
        )

    def _duplicates(self, plan: DuplicateCandidatesPlan) -> AnalysisResult:
        period = plan.period.resolve(self._today) if plan.period else None
        flt = TransactionFilter(
            date_from=period.start if period else None, date_to=period.end if period else None
        )
        txns = self._txns.fetch_all(flt)
        by_id = {t.id: t for t in txns}
        pairs = find_duplicate_pairs(txns)
        table = []
        for p in pairs:
            first, second = by_id[p.first_id], by_id[p.second_id]
            table.append(
                (
                    second.normalized_description,
                    second.amount.format(),
                    first.txn_date.isoformat(),
                    second.txn_date.isoformat(),
                    "same file" if p.same_import else "different files",
                    f"{p.chain_length} in a row",
                )
            )
        ids = tuple(i for p in pairs for i in (p.first_id, p.second_id))
        unique = tuple(dict.fromkeys(ids))
        return AnalysisResult(
            "duplicate_candidates",
            "Possible duplicate charges",
            "Charges with the same account, amount and description within 2 days. These are "
            "candidates only; repeated purchases are often genuine. Nothing is deleted.",
            (period,) if period else (),
            None,
            (Figure("Candidate pairs", str(len(pairs))),),
            ("Description", "Amount", "First", "Second", "Source", "Run"),
            tuple(table),
            self._evidence(unique),
            len(unique),
        )

    def _recurring(self, plan: RecurringChangesPlan) -> AnalysisResult:
        txns = self._txns.fetch_all()
        candidates = detect_recurring(txns)
        changes = amount_changes(candidates, threshold_pct=plan.min_change_pct)
        table = []
        for ch in changes:
            c = ch.candidate
            before = Money(ch.previous_typical_minor, c.currency)
            after = Money(ch.latest_minor, c.currency)
            table.append(
                (
                    c.merchant_key,
                    c.cadence_label,
                    before.format(),
                    after.format(),
                    _pct_text(
                        abs(ch.latest_minor) - abs(ch.previous_typical_minor),
                        ch.previous_typical_minor,
                    ),
                )
            )
        ids = tuple(i for ch in changes for i in ch.candidate.transaction_ids)
        return AnalysisResult(
            "recurring_changes",
            "Recurring payments that went up",
            f"Recurring payments (3+ similar charges at a regular interval) whose latest amount "
            f"is at least {plan.min_change_pct}% above their earlier typical amount.",
            (),
            None,
            (
                Figure("Recurring payments found", str(len(candidates))),
                Figure("With an increase", str(len(changes))),
            ),
            ("Merchant", "Cadence", "Earlier", "Latest", "Change"),
            tuple(table),
            self._evidence(ids[-EVIDENCE_ROWS_MAX:]),
            len(ids),
            notes=("Only payments with stable earlier amounts are compared.",),
        )

    def _drivers(self, plan: ExpenseChangeDriversPlan) -> AnalysisResult:
        a, b = plan.period_a.resolve(self._today), plan.period_b.resolve(self._today)
        currency = self._currency(plan.currency, [a, b])
        title = "What changed between two periods"
        if currency is None:
            return self._empty("expense_change_drivers", title, [a, b])
        x, y = self._metric(a, currency, "expenses"), self._metric(b, currency, "expenses")
        before = {r.name: r.amount for r in self._analyzer.category_spend(a, currency)}
        after = {r.name: r.amount for r in self._analyzer.category_spend(b, currency)}
        zero = Money.zero(currency)
        deltas = sorted(
            ((n, after.get(n, zero) - before.get(n, zero)) for n in set(before) | set(after)),
            key=lambda r: (-abs(r[1].minor), r[0]),
        )
        deltas = [d for d in deltas if d[1].minor != 0][:5]
        mb = {r.merchant_key: r.total for r in self._analyzer.merchant_summary(a, currency)}
        ma = {r.merchant_key: r.total for r in self._analyzer.merchant_summary(b, currency)}
        mdeltas = sorted(
            ((k, ma.get(k, zero) - mb.get(k, zero)) for k in set(mb) | set(ma)),
            key=lambda r: (-abs(r[1].minor), r[0] or ""),
        )
        mdeltas = [d for d in mdeltas if d[1].minor != 0][:5]
        table = [("Category", n, _signed(d)) for n, d in deltas] + [
            ("Merchant", k or "(unknown)", _signed(d)) for k, d in mdeltas
        ]
        ids = tuple(
            i for r in self._analyzer.merchant_summary(b, currency)[:5] for i in r.transaction_ids
        )
        return AnalysisResult(
            "expense_change_drivers",
            title,
            "Biggest differences in spending per category and per merchant between the two "
            "periods. These are associations in the data, not causes.",
            (a, b),
            currency,
            (
                Figure(f"Expenses ({a.label()})", x.format()),
                Figure(f"Expenses ({b.label()})", y.format()),
                Figure("Change", _signed(y - x)),
            ),
            ("Kind", "Name", "Change"),
            tuple(table),
            self._evidence(ids),
            self._analyzer.summary(a, currency).transaction_count
            + self._analyzer.summary(b, currency).transaction_count,
            notes=("These changes describe what differs; they do not explain why.",),
        )

    def _summary(self, plan: SpendingSummaryPlan) -> AnalysisResult:
        period = plan.period.resolve(self._today)
        currency = self._currency(plan.currency, [period])
        if currency is None:
            return self._empty("spending_summary", "Summary", [period])
        s = self._analyzer.summary(period, currency)
        cats = self._analyzer.category_spend(period, currency)[:5]
        merchants = self._analyzer.merchant_summary(period, currency)[:5]
        table = [("Category", r.name, r.amount.format()) for r in cats] + [
            ("Merchant", r.merchant_key or "(unknown)", r.total.format()) for r in merchants
        ]
        warnings = []
        if not s.uncategorized_out.is_zero or not s.uncategorized_in.is_zero:
            warnings.append(
                "Some transactions are uncategorized; they are shown separately and "
                "uncategorized money in is not counted as income."
            )
        ids = tuple(i for r in cats for i in r.transaction_ids)
        return AnalysisResult(
            "spending_summary",
            "Summary",
            "Income, expenses and net cash flow as defined in the analytics (transfers between "
            "your accounts excluded).",
            (period,),
            currency,
            (
                Figure("Income", s.income.format()),
                Figure("Expenses", s.expenses.format()),
                Figure("Net cash flow", _signed(s.net)),
                Figure("Cash withdrawn", s.cash_withdrawn.format()),
            ),
            ("Kind", "Name", "Amount"),
            tuple(table),
            self._evidence(ids),
            s.transaction_count,
            tuple(warnings),
        )

    def _merchant(self, plan: MerchantSummaryPlan) -> AnalysisResult:
        period = plan.period.resolve(self._today)
        currency = self._currency(plan.currency, [period])
        title = f"Spending at '{plan.merchant}'"
        if currency is None:
            return self._empty("merchant_summary", title, [period])
        needle = " ".join(plan.merchant.upper().split())
        rows = [
            r
            for r in self._analyzer.merchant_summary(period, currency)
            if r.merchant_key and needle in r.merchant_key
        ]
        total = sum((r.total.minor for r in rows), 0)
        ids = tuple(i for r in rows for i in r.transaction_ids)
        notes = [] if rows else ["No spending matched that name in the period."]
        if len(rows) > 1:
            notes.append(f"{len(rows)} merchant names matched; they are added together.")
        return AnalysisResult(
            "merchant_summary",
            title,
            "Money paid to merchants whose name contains the text you gave (plain text match).",
            (period,),
            currency,
            (
                Figure("Total paid", Money(total, currency).format()),
                Figure("Transactions", str(len(ids))),
            ),
            ("Merchant", "Total", "Transactions"),
            tuple((r.merchant_key or "", r.total.format(), str(r.count)) for r in rows),
            self._evidence(ids),
            len(ids),
            notes=tuple(notes),
        )


def _row(txn: CanonicalTransaction) -> EvidenceRow:
    return EvidenceRow(txn.id, txn.txn_date, txn.normalized_description, txn.amount.format())
