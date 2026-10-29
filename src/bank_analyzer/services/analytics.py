"""Deterministic financial analytics.

Every figure is computed in Python from integer minor units; no model is involved. Each metric
carries its definition, period, currency and the ids of the transactions behind it, and currencies
are never combined. The treatment of transfers, refunds, cash withdrawals and savings follows
docs/05 section 2, so money is not counted twice.

How a transaction is bucketed (see ``classify``):

* part of a matched own-account transfer pair  -> excluded from income and spending
* no category                                  -> uncategorized (money out counts as spending and is
                                                  flagged; money in is *not* called income and is flagged)
* category role income                         -> income
* category role expense                        -> spending
* category role refund                         -> reduces spending (against the original purchase's
                                                  category when it can be linked, otherwise "unlinked")
* category role cash_withdrawal                -> reported separately (not spending, not income)
* category role transfer, not matched          -> excluded, but reported as an unmatched transfer
* category role savings                        -> excluded, unless the user opts to count it as spending

Identity (tested): raw net = income - spending - cash withdrawn + savings + matched transfers
+ unmatched transfers + uncategorized money in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from bank_analyzer.database.repositories import (
    CategoryRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.models import CanonicalTransaction, Category, CategoryRole
from bank_analyzer.domain.money import Money
from bank_analyzer.domain.periods import DateRange, months_in, previous_month
from bank_analyzer.services.normalization import merchant_key

_PCT = Decimal("0.0001")
_REFUND_WORDS = frozenset({"REFUND", "REVERSAL", "CHARGEBACK", "RETURN", "CREDIT"})
UNCATEGORIZED_NAME = "Uncategorized"
UNLINKED_REFUNDS_NAME = "Unlinked refunds"


@dataclass(frozen=True, slots=True)
class AnalyticsConfig:
    """Tunable rules. Defaults are documented in docs/05."""

    refund_window_days: int = 90  # a refund may link to a purchase at most this many days earlier
    savings_as_expense: bool = False  # count savings/investment transfers as spending
    max_unresolved_ratio: Decimal = Decimal(
        "0.05"
    )  # savings rate needs unresolved <= this x income
    min_trend_months: int = 3  # a trend needs at least this many months of data


class Bucket(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    REFUND_LINKED = "refund_linked"
    REFUND_UNLINKED = "refund_unlinked"
    CASH_WITHDRAWAL = "cash_withdrawal"
    TRANSFER_MATCHED = "transfer_matched"
    TRANSFER_UNMATCHED = "transfer_unmatched"
    SAVINGS = "savings"
    UNCATEGORIZED_IN = "uncategorized_in"
    UNCATEGORIZED_OUT = "uncategorized_out"


_SPENDING_BUCKETS = frozenset(
    {Bucket.EXPENSE, Bucket.REFUND_LINKED, Bucket.REFUND_UNLINKED, Bucket.UNCATEGORIZED_OUT}
)


@dataclass(frozen=True, slots=True)
class Classified:
    """A transaction with its analytics bucket. ``category_id`` is the category spending is
    attributed to (for a linked refund, the original purchase's category)."""

    txn: CanonicalTransaction
    bucket: Bucket
    category_id: str | None
    linked_to: str | None = None  # refund -> original purchase id


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One figure with everything needed to trust it."""

    name: str
    definition: str
    period: DateRange
    currency: str
    value: Money | Decimal | None
    unavailable_reason: str | None
    transaction_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CashFlowSummary:
    """All buckets for one period and currency, so related metrics always agree."""

    period: DateRange
    currency: str
    income: Money
    expenses: Money  # spending net of refunds, uncategorized money out included
    net: Money  # income - expenses
    cash_withdrawn: Money
    refunds_netted: Money
    savings_net: Money  # signed
    matched_transfers_net: Money  # signed; 0 when both legs are inside the period
    unmatched_transfers_in: Money
    unmatched_transfers_out: Money
    uncategorized_in: Money
    uncategorized_out: Money  # already included in expenses; shown so it can be reviewed
    raw_credits: Money
    raw_debits: Money  # negative or zero
    raw_net: Money
    ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    transaction_count: int = 0


@dataclass(frozen=True, slots=True)
class CategorySpend:
    category_id: str | None
    name: str
    amount: Money  # spending positive; a net refund balance can make it negative
    count: int
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Change:
    """Change from the previous month. ``pct`` is None when it would be meaningless."""

    month: str
    current: Money
    previous: Money | None
    delta: Money | None
    pct: Decimal | None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MonthlyFlow:
    month: str
    summary: CashFlowSummary
    has_data: bool
    savings_rate: MetricResult


@dataclass(frozen=True, slots=True)
class SeriesResult:
    name: str
    definition: str
    period: DateRange
    currency: str
    points: tuple[tuple[str, Money], ...]
    unavailable_reason: str | None
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MerchantSummary:
    merchant_key: str | None
    count: int
    total: Money  # positive amount of money paid
    average: Decimal  # in major units, exact then rounded to 4 places
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RefundReport:
    period: DateRange
    currency: str
    linked: tuple[tuple[str, str], ...]  # (refund id, original purchase id)
    unlinked_ids: tuple[str, ...]
    linked_total: Money
    unlinked_total: Money


def _refund_compare_key(txn: CanonicalTransaction) -> str | None:
    """Merchant key of a refund with words like REFUND removed, to compare with the purchase."""
    tokens = [t for t in txn.normalized_description.split() if t not in _REFUND_WORDS]
    return merchant_key(" ".join(tokens))


def link_refunds(
    refunds: Sequence[CanonicalTransaction],
    candidates: Sequence[CanonicalTransaction],
    window_days: int,
) -> dict[str, str]:
    """Match each refund to the latest earlier purchase with the same account, currency, merchant
    and exactly the refunded amount. Each purchase is used once. Partial refunds stay unlinked
    (a documented limitation) rather than being matched on a guess."""
    used: set[str] = set()
    links: dict[str, str] = {}
    for refund in sorted(refunds, key=lambda t: (t.txn_date, t.id)):
        key = _refund_compare_key(refund)
        if key is None or refund.amount_minor <= 0:
            continue
        best: CanonicalTransaction | None = None
        for original in candidates:
            if (
                original.id in used
                or original.account_id != refund.account_id
                or original.currency != refund.currency
                or original.amount_minor != -refund.amount_minor
                or original.merchant_key != key
                or not 0 <= (refund.txn_date - original.txn_date).days <= window_days
            ):
                continue
            if best is None or original.txn_date > best.txn_date:
                best = original
        if best is not None:
            used.add(best.id)
            links[refund.id] = best.id
    return links


def classify(
    txns: Sequence[CanonicalTransaction],
    categories: Mapping[str, Category],
    config: AnalyticsConfig,
) -> list[Classified]:
    """Bucket every transaction (see the module docstring for the rules)."""

    def role_of(txn: CanonicalTransaction) -> CategoryRole | None:
        if txn.category_id is None:
            return None
        category = categories.get(txn.category_id)
        return category.role if category else None

    purchases = [t for t in txns if t.amount_minor < 0 and role_of(t) != CategoryRole.REFUND]
    refunds = [t for t in txns if t.amount_minor > 0 and role_of(t) == CategoryRole.REFUND]
    by_id = {t.id: t for t in txns}
    links = link_refunds(refunds, purchases, config.refund_window_days)

    out: list[Classified] = []
    for txn in txns:
        role = role_of(txn)
        amount = txn.amount_minor
        if txn.transfer_group_id is not None:
            out.append(Classified(txn, Bucket.TRANSFER_MATCHED, None))
        elif txn.category_id is None or role is None:
            bucket = Bucket.UNCATEGORIZED_IN if amount > 0 else Bucket.UNCATEGORIZED_OUT
            out.append(Classified(txn, bucket, None))
        elif role == CategoryRole.INCOME:
            out.append(Classified(txn, Bucket.INCOME, txn.category_id))
        elif role == CategoryRole.EXPENSE:
            out.append(Classified(txn, Bucket.EXPENSE, txn.category_id))
        elif role == CategoryRole.REFUND:
            original_id = links.get(txn.id)
            if original_id is not None:
                out.append(
                    Classified(
                        txn, Bucket.REFUND_LINKED, by_id[original_id].category_id, original_id
                    )
                )
            else:
                out.append(Classified(txn, Bucket.REFUND_UNLINKED, None))
        elif role == CategoryRole.CASH_WITHDRAWAL:
            out.append(Classified(txn, Bucket.CASH_WITHDRAWAL, txn.category_id))
        elif role == CategoryRole.TRANSFER:
            out.append(Classified(txn, Bucket.TRANSFER_UNMATCHED, txn.category_id))
        elif config.savings_as_expense:  # CategoryRole.SAVINGS, opted in
            out.append(Classified(txn, Bucket.EXPENSE, txn.category_id))
        else:
            out.append(Classified(txn, Bucket.SAVINGS, txn.category_id))
    return out


def _pct(delta: int, base: int) -> Decimal | None:
    if base == 0:
        return None
    return (Decimal(delta) / Decimal(abs(base))).quantize(_PCT, rounding=ROUND_HALF_EVEN)


class Analyzer:
    """Metrics over a set of transactions. Build once, ask for any period and currency."""

    def __init__(
        self,
        txns: Iterable[CanonicalTransaction],
        categories: Mapping[str, Category],
        config: AnalyticsConfig | None = None,
    ) -> None:
        self.config = config or AnalyticsConfig()
        self.categories = dict(categories)
        live = [t for t in txns if not t.is_voided]
        self._classified = classify(live, self.categories, self.config)

    @classmethod
    def from_db(
        cls,
        conn: sqlite3.Connection,
        *,
        account_id: str | None = None,
        config: AnalyticsConfig | None = None,
    ) -> Analyzer:
        """Load every live transaction (all history, so refunds can link to older purchases)."""
        txns = TransactionRepository(conn).fetch_all(TransactionFilter(account_id=account_id))
        categories = {c.id: c for c in CategoryRepository(conn).list(active_only=False)}
        return cls(txns, categories, config)

    # -- selection ---------------------------------------------------------------------------

    def currencies(self, period: DateRange | None = None) -> list[str]:
        """Currencies present (in ``period`` if given). Totals are always per currency."""
        return sorted(
            {
                c.txn.currency
                for c in self._classified
                if period is None or period.contains(c.txn.txn_date)
            }
        )

    def _rows(self, period: DateRange, currency: str) -> list[Classified]:
        return [
            c
            for c in self._classified
            if c.txn.currency == currency and period.contains(c.txn.txn_date)
        ]

    # -- the cash-flow summary and the metrics derived from it ----------------------------------

    def summary(self, period: DateRange, currency: str) -> CashFlowSummary:
        """Bucket totals for one period and currency (all amounts are Money in ``currency``)."""
        rows = self._rows(period, currency)
        totals = dict.fromkeys(
            (
                "income",
                "expenses",
                "cash",
                "refunds",
                "savings",
                "matched",
                "unm_in",
                "unm_out",
                "unc_in",
                "unc_out",
                "credits",
                "debits",
            ),
            0,
        )
        ids: dict[str, list[str]] = {
            k: []
            for k in (
                "income",
                "expenses",
                "refunds",
                "cash_withdrawals",
                "transfers_matched",
                "transfers_unmatched",
                "savings",
                "uncategorized_in",
                "uncategorized_out",
            )
        }
        for c in rows:
            amount = c.txn.amount_minor
            totals["credits" if amount > 0 else "debits"] += amount if amount != 0 else 0
            if c.bucket == Bucket.INCOME:
                totals["income"] += amount
                ids["income"].append(c.txn.id)
            elif c.bucket in _SPENDING_BUCKETS:
                totals["expenses"] -= amount
                ids["expenses"].append(c.txn.id)
                if c.bucket in (Bucket.REFUND_LINKED, Bucket.REFUND_UNLINKED):
                    totals["refunds"] += amount
                    ids["refunds"].append(c.txn.id)
                elif c.bucket == Bucket.UNCATEGORIZED_OUT:
                    totals["unc_out"] -= amount
                    ids["uncategorized_out"].append(c.txn.id)
            elif c.bucket == Bucket.CASH_WITHDRAWAL:
                totals["cash"] -= amount
                ids["cash_withdrawals"].append(c.txn.id)
            elif c.bucket == Bucket.TRANSFER_MATCHED:
                totals["matched"] += amount
                ids["transfers_matched"].append(c.txn.id)
            elif c.bucket == Bucket.TRANSFER_UNMATCHED:
                totals["unm_in" if amount > 0 else "unm_out"] += abs(amount)
                ids["transfers_unmatched"].append(c.txn.id)
            elif c.bucket == Bucket.SAVINGS:
                totals["savings"] += amount
                ids["savings"].append(c.txn.id)
            elif c.bucket == Bucket.UNCATEGORIZED_IN:
                totals["unc_in"] += amount
                ids["uncategorized_in"].append(c.txn.id)

        def m(key: str) -> Money:
            return Money(totals[key], currency)

        return CashFlowSummary(
            period=period,
            currency=currency,
            income=m("income"),
            expenses=m("expenses"),
            net=Money(totals["income"] - totals["expenses"], currency),
            cash_withdrawn=m("cash"),
            refunds_netted=m("refunds"),
            savings_net=m("savings"),
            matched_transfers_net=m("matched"),
            unmatched_transfers_in=m("unm_in"),
            unmatched_transfers_out=m("unm_out"),
            uncategorized_in=m("unc_in"),
            uncategorized_out=m("unc_out"),
            raw_credits=m("credits"),
            raw_debits=m("debits"),
            raw_net=Money(totals["credits"] + totals["debits"], currency),
            ids={k: tuple(v) for k, v in ids.items()},
            transaction_count=len(rows),
        )

    def _metric(
        self,
        name: str,
        definition: str,
        s: CashFlowSummary,
        value: Money | Decimal | None,
        ids: tuple[str, ...],
        warnings: tuple[str, ...] = (),
        reason: str | None = None,
    ) -> MetricResult:
        return MetricResult(name, definition, s.period, s.currency, value, reason, ids, warnings)

    def total_credits(self, period: DateRange, currency: str) -> MetricResult:
        s = self.summary(period, currency)
        ids = tuple(c.txn.id for c in self._rows(period, currency) if c.txn.amount_minor > 0)
        return self._metric(
            "Total credits",
            "Sum of every positive amount in the period, including transfers (raw view).",
            s,
            s.raw_credits,
            ids,
        )

    def total_debits(self, period: DateRange, currency: str) -> MetricResult:
        s = self.summary(period, currency)
        ids = tuple(c.txn.id for c in self._rows(period, currency) if c.txn.amount_minor < 0)
        return self._metric(
            "Total debits",
            "Sum of every negative amount in the period, including transfers (raw view). "
            "Shown as a negative number.",
            s,
            s.raw_debits,
            ids,
        )

    def income(self, period: DateRange, currency: str) -> MetricResult:
        s = self.summary(period, currency)
        warnings = self._unresolved_warnings(s)
        return self._metric(
            "Income",
            "Money in under categories with the Income role. Transfers between your own accounts, "
            "refunds and uncategorized money in are not income.",
            s,
            s.income,
            s.ids["income"],
            warnings,
        )

    def expenses(self, period: DateRange, currency: str) -> MetricResult:
        s = self.summary(period, currency)
        return self._metric(
            "Expenses",
            "Money out under spending categories plus uncategorized money out, minus refunds. "
            "Excludes own-account transfers, cash withdrawals and savings.",
            s,
            s.expenses,
            s.ids["expenses"],
            self._unresolved_warnings(s),
        )

    def net_cash_flow(self, period: DateRange, currency: str) -> MetricResult:
        s = self.summary(period, currency)
        ids = tuple(dict.fromkeys(s.ids["income"] + s.ids["expenses"]))
        return self._metric(
            "Net cash flow",
            "Income minus expenses (definitions above). The raw net of every transaction is "
            "available separately.",
            s,
            s.net,
            ids,
            self._unresolved_warnings(s),
        )

    def cash_withdrawals(self, period: DateRange, currency: str) -> MetricResult:
        s = self.summary(period, currency)
        return self._metric(
            "Cash withdrawn",
            "Money taken out as cash. Reported on its own and not counted as spending, so it is "
            "not double counted if you later record what the cash was spent on.",
            s,
            s.cash_withdrawn,
            s.ids["cash_withdrawals"],
        )

    @staticmethod
    def _unresolved_warnings(s: CashFlowSummary) -> tuple[str, ...]:
        warnings = []
        if s.uncategorized_in.minor:
            warnings.append(
                f"{s.uncategorized_in.format()} of money in is uncategorized and not counted as income."
            )
        if s.uncategorized_out.minor:
            warnings.append(
                f"{s.uncategorized_out.format()} of money out is uncategorized (counted as spending)."
            )
        unmatched = s.unmatched_transfers_in.minor + s.unmatched_transfers_out.minor
        if unmatched:
            warnings.append(
                "Some transfers have no matching leg in another account and are excluded: "
                f"{Money(unmatched, s.currency).format()} in total."
            )
        return tuple(warnings)

    # -- savings rate ------------------------------------------------------------------------

    def savings_rate(self, period: DateRange, currency: str) -> MetricResult:
        """(Income - expenses) / income, only when that is meaningful, otherwise why not."""
        s = self.summary(period, currency)
        definition = (
            "(Income - Expenses) / Income for whole calendar months, using the definitions of "
            "Income and Expenses above."
        )
        warnings: list[str] = []
        others = [c for c in self.currencies(period) if c != currency]
        if others:
            warnings.append(
                f"Other currencies in this period ({', '.join(others)}) are not included."
            )

        def unavailable(reason: str) -> MetricResult:
            return self._metric(
                "Savings rate", definition, s, None, (), tuple(warnings), reason=reason
            )

        if not period.is_whole_months:
            return unavailable("The period is not a whole number of calendar months.")
        if s.transaction_count == 0:
            return unavailable("There are no transactions in this period.")
        if s.income.minor <= 0:
            return unavailable("There is no income in this period, so a rate cannot be computed.")
        unresolved = (
            s.unmatched_transfers_in.minor
            + s.unmatched_transfers_out.minor
            + s.uncategorized_in.minor
        )
        if Decimal(unresolved) > self.config.max_unresolved_ratio * Decimal(s.income.minor):
            return unavailable(
                "Unmatched transfers and uncategorized money in "
                f"({Money(unresolved, currency).format()}) are more than "
                f"{self.config.max_unresolved_ratio:.0%} of income, so the rate could be misleading. "
                "Categorize them or link the transfers first."
            )
        rate = (Decimal(s.net.minor) / Decimal(s.income.minor)).quantize(
            _PCT, rounding=ROUND_HALF_EVEN
        )
        ids = tuple(dict.fromkeys(s.ids["income"] + s.ids["expenses"]))
        return self._metric("Savings rate", definition, s, rate, ids, tuple(warnings))

    # -- monthly view, comparisons and trends ------------------------------------------------

    def monthly(self, period: DateRange, currency: str) -> list[MonthlyFlow]:
        """One entry per calendar month touched by ``period`` (months without data included)."""
        flows = []
        for key in months_in(period):
            month_period = DateRange.month(key)
            clipped = DateRange(
                max(month_period.start, period.start), min(month_period.end, period.end)
            )
            summary = self.summary(clipped, currency)
            flows.append(
                MonthlyFlow(
                    month=key,
                    summary=summary,
                    has_data=summary.transaction_count > 0,
                    savings_rate=self.savings_rate(clipped, currency),
                )
            )
        return flows

    def month_over_month(
        self, period: DateRange, currency: str, metric: str = "expenses"
    ) -> list[Change]:
        """Each month against the one before it. A missing month is never assumed to be zero."""
        if metric not in {"expenses", "income", "net"}:
            raise ValueError("metric must be 'expenses', 'income' or 'net'")
        flows = {f.month: f for f in self.monthly(period, currency)}
        changes: list[Change] = []
        for key, flow in flows.items():
            current: Money = getattr(flow.summary, metric)
            if not flow.has_data:
                changes.append(
                    Change(key, current, None, None, None, "There are no transactions this month.")
                )
                continue
            prior = flows.get(previous_month(key))
            if prior is None or not prior.has_data:
                changes.append(
                    Change(
                        key,
                        current,
                        None,
                        None,
                        None,
                        "No data for the previous month, so there is nothing to compare with.",
                    )
                )
                continue
            previous: Money = getattr(prior.summary, metric)
            delta = current - previous
            pct = _pct(delta.minor, previous.minor)
            note = (
                "The previous month was zero, so a percentage is not defined."
                if pct is None
                else None
            )
            changes.append(Change(key, current, previous, delta, pct, note))
        return changes

    def spending_trend(self, period: DateRange, currency: str) -> SeriesResult:
        """Monthly expenses, only when there are enough months with data to call it a trend."""
        flows = [f for f in self.monthly(period, currency) if f.has_data]
        points = tuple((f.month, f.summary.expenses) for f in flows)
        reason = None
        if len(points) < self.config.min_trend_months:
            reason = (
                f"A trend needs at least {self.config.min_trend_months} months with data; "
                f"this period has {len(points)}."
            )
        ids = tuple(i for f in flows for i in f.summary.ids["expenses"])
        return SeriesResult(
            "Monthly expenses",
            "Expenses (as defined above) for each calendar month with data.",
            period,
            currency,
            points,
            reason,
            ids,
        )

    # -- breakdowns --------------------------------------------------------------------------

    def category_spend(self, period: DateRange, currency: str) -> list[CategorySpend]:
        """Spending per category, refunds netted against their original purchase's category."""
        groups: dict[str | None, list[Classified]] = {}
        for c in self._rows(period, currency):
            if c.bucket not in _SPENDING_BUCKETS:
                continue
            key = "__unlinked__" if c.bucket == Bucket.REFUND_UNLINKED else c.category_id
            groups.setdefault(key, []).append(c)
        result = []
        for key, rows in groups.items():
            if key == "__unlinked__":
                name = UNLINKED_REFUNDS_NAME
                category_id = None
            elif key is None:
                name = UNCATEGORIZED_NAME
                category_id = None
            else:
                name = self.categories[key].name if key in self.categories else "(unknown)"
                category_id = key
            result.append(
                CategorySpend(
                    category_id=category_id,
                    name=name,
                    amount=Money(-sum(r.txn.amount_minor for r in rows), currency),
                    count=len(rows),
                    transaction_ids=tuple(r.txn.id for r in rows),
                )
            )
        return sorted(result, key=lambda r: (-r.amount.minor, r.name))

    def largest_transactions(
        self, period: DateRange, currency: str, *, n: int = 5, direction: str = "out"
    ) -> list[CanonicalTransaction]:
        """The ``n`` biggest transactions by absolute amount, excluding matched transfers."""
        if direction not in {"out", "in", "any"}:
            raise ValueError("direction must be 'out', 'in' or 'any'")
        rows = [
            c.txn
            for c in self._rows(period, currency)
            if c.bucket != Bucket.TRANSFER_MATCHED
            and (
                direction == "any"
                or (direction == "out" and c.txn.amount_minor < 0)
                or (direction == "in" and c.txn.amount_minor > 0)
            )
        ]
        rows.sort(key=lambda t: (-abs(t.amount_minor), t.txn_date, t.id))
        return rows[: max(0, n)]

    def merchant_summary(self, period: DateRange, currency: str) -> list[MerchantSummary]:
        """Spending per merchant (money out in spending buckets), biggest first."""
        groups: dict[str | None, list[CanonicalTransaction]] = {}
        for c in self._rows(period, currency):
            if c.bucket in (Bucket.EXPENSE, Bucket.UNCATEGORIZED_OUT):
                groups.setdefault(c.txn.merchant_key, []).append(c.txn)
        result = []
        for key, txns in groups.items():
            total = -sum(t.amount_minor for t in txns)
            average = (Money(total, currency).to_decimal() / len(txns)).quantize(
                _PCT, rounding=ROUND_HALF_EVEN
            )
            result.append(
                MerchantSummary(
                    key, len(txns), Money(total, currency), average, tuple(t.id for t in txns)
                )
            )
        return sorted(result, key=lambda r: (-r.total.minor, r.merchant_key or ""))

    def refunds(self, period: DateRange, currency: str) -> RefundReport:
        linked, unlinked = [], []
        linked_total = unlinked_total = 0
        for c in self._rows(period, currency):
            if c.bucket == Bucket.REFUND_LINKED and c.linked_to:
                linked.append((c.txn.id, c.linked_to))
                linked_total += c.txn.amount_minor
            elif c.bucket == Bucket.REFUND_UNLINKED:
                unlinked.append(c.txn.id)
                unlinked_total += c.txn.amount_minor
        return RefundReport(
            period,
            currency,
            tuple(linked),
            tuple(unlinked),
            Money(linked_total, currency),
            Money(unlinked_total, currency),
        )
