"""Explainable, rule-based alerts. No model, no scoring, no claims of fraud.

Every alert names the rule that fired, the figures it compared and the baseline behind them, and
reads as "this may be legitimate". The user dismisses or confirms; nothing is frozen, blocked,
reported or sent anywhere.

Rules (thresholds live in ``AnomalyConfig``):

* A1 large amount      - a spending amount well above the usual for its category (or merchant)
* A2 frequency spike   - far more charges from one merchant this week than in earlier weeks
* A3 duplicate charge  - the same amount, merchant and account twice within a couple of days
* A4 recurring increase- a stable subscription or bill got more expensive
* A5 new merchant      - the first sizeable charge from a merchant not seen before
* A6 negative balance  - a deposit account's reported balance went below zero for the first time

A rule only fires when there is enough history to compare against; otherwise the run reports why
it stayed silent instead of guessing. New alerts cover only recent activity (``scan_days``), so a
first import of years of statements does not flood the user. Re-running never recreates an alert
the user already saw (open, dismissed or confirmed): each alert has a stable identity key.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

from bank_analyzer.database.connection import transaction
from bank_analyzer.database.repositories import (
    AccountRepository,
    AlertRepository,
    CategoryRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.models import (
    AccountType,
    AlertStatus,
    AnomalyAlert,
    CanonicalTransaction,
    Category,
)
from bank_analyzer.domain.money import Money
from bank_analyzer.services.analytics import AnalyticsConfig, Bucket, Classified, classify
from bank_analyzer.services.duplicates import find_duplicate_pairs
from bank_analyzer.services.reconciliation import source_order_key
from bank_analyzer.services.recurring import amount_changes, detect_recurring, dismissed_keys

A1, A2, A3, A4, A5, A6 = "A1", "A2", "A3", "A4", "A5", "A6"
RULE_NAMES = {
    A1: "large amount",
    A2: "frequency spike",
    A3: "possible duplicate charge",
    A4: "recurring payment increase",
    A5: "new merchant",
    A6: "negative balance",
}
_MAD_SCALE = Fraction(14826, 10000)  # makes MAD comparable to a standard deviation


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    scan_days: int = 90
    # A1
    large_min_history: int = 8
    large_window_days: int = 180
    large_k: int = 5
    large_min_ratio_pct: int = (
        150  # also at least this % of the median, so flat amounts do not trip
    )
    # A2
    freq_window_days: int = 7
    freq_min_windows: int = 8
    freq_k: int = 3
    freq_min_count: int = 3
    freq_min_active_windows: int = 3
    # A3
    duplicate_window_days: int = 2
    duplicate_max_chain: int = 3  # longer runs of look-alikes are habits, not duplicates
    duplicate_min_amount_minor: int = 0
    # A4
    price_change_pct: int = 10
    # A5
    new_merchant_lookback_days: int = 365
    new_merchant_min_history_days: int = 90
    new_merchant_min_amount_minor: int = 2000
    new_merchant_recent_days: int = 30
    # A6
    negative_min_history: int = 10
    overdraft_limit_minor: int = 0


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    rule_id: str
    transaction_id: str | None
    related_ids: tuple[str, ...]
    details: dict[str, Any]
    dedupe_key: str


@dataclass(frozen=True, slots=True)
class DetectionContext:
    classified: Sequence[Classified]
    categories: Mapping[str, Category]
    account_types: Mapping[str, AccountType]
    as_of: date
    dismissed_recurring: frozenset[tuple[str, str]] = frozenset()

    @property
    def txns(self) -> list[CanonicalTransaction]:
        return [c.txn for c in self.classified]

    @property
    def spending(self) -> list[CanonicalTransaction]:
        return [
            c.txn
            for c in self.classified
            if c.bucket in (Bucket.EXPENSE, Bucket.UNCATEGORIZED_OUT) and c.txn.amount_minor < 0
        ]


def _median(values: Sequence[int | Fraction]) -> Fraction:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[mid])
    return Fraction(ordered[mid - 1] + ordered[mid], 2)


def _recent(day: date, ctx: DetectionContext, cfg: AnomalyConfig) -> bool:
    return ctx.as_of - timedelta(days=cfg.scan_days) <= day <= ctx.as_of


# ------------------------------------------------------------------------------------ A1
def detect_large_amounts(
    ctx: DetectionContext, cfg: AnomalyConfig
) -> tuple[list[AlertCandidate], str | None]:
    groups: dict[tuple[str, str, str], list[CanonicalTransaction]] = defaultdict(list)
    for txn in ctx.spending:
        if txn.category_id:
            groups[("category", txn.category_id, txn.currency)].append(txn)
        elif txn.merchant_key:
            groups[("merchant", txn.merchant_key, txn.currency)].append(txn)
    found: list[AlertCandidate] = []
    enough_anywhere = False
    for (kind, ident, currency), members in groups.items():
        members.sort(key=lambda t: (t.txn_date, t.id))
        dates = [t.txn_date for t in members]
        if len(members) > cfg.large_min_history:
            enough_anywhere = True
        for target in members:
            if not _recent(target.txn_date, ctx, cfg):
                continue
            lo = bisect_left(dates, target.txn_date - timedelta(days=cfg.large_window_days))
            hi = bisect_left(dates, target.txn_date)  # strictly earlier days only
            prior = members[lo:hi]
            if len(prior) < cfg.large_min_history:
                continue
            amounts = sorted(abs(p.amount_minor) for p in prior)
            median = _median(amounts)
            mad = _median([abs(Fraction(a) - median) for a in amounts])
            p95 = amounts[(95 * len(amounts) + 99) // 100 - 1]
            threshold = max(median + cfg.large_k * _MAD_SCALE * mad, Fraction(p95))
            size = abs(target.amount_minor)
            if size > threshold and size * 100 >= median * cfg.large_min_ratio_pct:
                label = (
                    ctx.categories[ident].name
                    if kind == "category" and ident in ctx.categories
                    else ident
                )
                found.append(
                    AlertCandidate(
                        A1,
                        target.id,
                        tuple(p.id for p in prior[-5:]),
                        {
                            "amount_minor": size,
                            "currency": currency,
                            "group_kind": kind,
                            "group_label": label,
                            "baseline_count": len(prior),
                            "median_minor": round(median),
                            "mad_minor": round(mad),
                            "p95_minor": p95,
                            "threshold_minor": round(threshold),
                            "k": cfg.large_k,
                            "window_days": cfg.large_window_days,
                        },
                        f"A1:{target.id}",
                    )
                )
    note = (
        None
        if enough_anywhere or found
        else (
            f"needs at least {cfg.large_min_history} earlier transactions in a category or merchant"
        )
    )
    return found, note


# ------------------------------------------------------------------------------------ A2
def detect_frequency_spikes(
    ctx: DetectionContext, cfg: AnomalyConfig
) -> tuple[list[AlertCandidate], str | None]:
    all_txns = ctx.txns
    if not all_txns:
        return [], "no transactions"
    earliest = min(t.txn_date for t in all_txns)
    needed = cfg.freq_min_windows * cfg.freq_window_days
    have = (ctx.as_of - earliest).days
    if have < needed:
        return [], f"needs at least {needed} days of history; have {have}"
    window = cfg.freq_window_days
    current_start = ctx.as_of - timedelta(days=window - 1)
    prior_windows = (current_start - earliest).days // window
    groups: dict[tuple[str, str], list[CanonicalTransaction]] = defaultdict(list)
    for txn in ctx.spending:
        if txn.merchant_key:
            groups[(txn.merchant_key, txn.currency)].append(txn)
    found: list[AlertCandidate] = []
    for (merchant, currency), members in groups.items():
        now = [t for t in members if current_start <= t.txn_date <= ctx.as_of]
        if len(now) < cfg.freq_min_count:
            continue
        counts = [0] * prior_windows
        for t in members:
            if t.txn_date < current_start:
                index = ((current_start - t.txn_date).days - 1) // window  # 0 = window just before
                if index < prior_windows:
                    counts[index] += 1
        if sum(1 for c in counts if c) < cfg.freq_min_active_windows:
            continue
        mean = Fraction(sum(counts), prior_windows)
        variance = sum((Fraction(c) - mean) ** 2 for c in counts) / prior_windows
        current = len(now)
        if current > mean and (current - mean) ** 2 > cfg.freq_k**2 * variance:
            now.sort(key=lambda t: (t.txn_date, t.id))
            found.append(
                AlertCandidate(
                    A2,
                    now[-1].id,
                    tuple(t.id for t in now[:-1]),
                    {
                        "merchant_key": merchant,
                        "currency": currency,
                        "count": current,
                        "window_days": window,
                        "window_start": current_start.isoformat(),
                        "prior_windows": prior_windows,
                        "prior_mean": f"{float(mean):.2f}",
                        "prior_std": f"{float(variance) ** 0.5:.2f}",
                        "k": cfg.freq_k,
                    },
                    f"A2:{merchant}:{currency}:{current_start.isoformat()}",
                )
            )
    return found, None


# ------------------------------------------------------------------------------------ A3
def detect_duplicate_charges(
    ctx: DetectionContext, cfg: AnomalyConfig
) -> tuple[list[AlertCandidate], str | None]:
    by_id = {t.id: t for t in ctx.spending}
    eligible = [t for t in by_id.values() if abs(t.amount_minor) >= cfg.duplicate_min_amount_minor]
    found = []
    for pair in find_duplicate_pairs(eligible, window_days=cfg.duplicate_window_days):
        second = by_id[pair.second_id]
        if pair.chain_length > cfg.duplicate_max_chain or not _recent(second.txn_date, ctx, cfg):
            continue
        found.append(
            AlertCandidate(
                A3,
                pair.second_id,
                (pair.first_id,),
                {
                    "amount_minor": abs(second.amount_minor),
                    "currency": second.currency,
                    "days_apart": pair.days_apart,
                    "same_import": pair.same_import,
                    "chain_length": pair.chain_length,
                    "window_days": cfg.duplicate_window_days,
                },
                f"A3:{pair.first_id}:{pair.second_id}",
            )
        )
    return found, None


# ------------------------------------------------------------------------------------ A4
def detect_recurring_increases(
    ctx: DetectionContext, cfg: AnomalyConfig
) -> tuple[list[AlertCandidate], str | None]:
    candidates = [
        c
        for c in detect_recurring(ctx.spending)
        if (c.merchant_key, c.currency) not in ctx.dismissed_recurring
        and _recent(c.last_date, ctx, cfg)
    ]
    found = []
    for change in amount_changes(candidates, threshold_pct=cfg.price_change_pct):
        c = change.candidate
        found.append(
            AlertCandidate(
                A4,
                c.transaction_ids[-1],
                (),
                {
                    "merchant_key": c.merchant_key,
                    "currency": c.currency,
                    "cadence": c.cadence_label,
                    "occurrences": c.occurrences,
                    "previous_typical_minor": abs(change.previous_typical_minor),
                    "latest_minor": abs(change.latest_minor),
                    "change_pct": f"{float(change.change_pct) * 100:.1f}",
                    "threshold_pct": cfg.price_change_pct,
                },
                f"A4:{c.transaction_ids[-1]}",
            )
        )
    return found, None


# ------------------------------------------------------------------------------------ A5
def detect_new_merchants(
    ctx: DetectionContext, cfg: AnomalyConfig
) -> tuple[list[AlertCandidate], str | None]:
    all_txns = ctx.txns
    if not all_txns:
        return [], "no transactions"
    earliest = min(t.txn_date for t in all_txns)
    groups: dict[tuple[str, str], list[CanonicalTransaction]] = defaultdict(list)
    for txn in ctx.spending:
        if txn.merchant_key:
            groups[(txn.merchant_key, txn.currency)].append(txn)
    found = []
    recent_from = ctx.as_of - timedelta(days=cfg.new_merchant_recent_days)
    history_days = (ctx.as_of - earliest).days
    for (merchant, currency), members in groups.items():
        members.sort(key=lambda t: (t.txn_date, t.id))
        for index, txn in enumerate(members):
            if not recent_from <= txn.txn_date <= ctx.as_of:
                continue
            if (txn.txn_date - earliest).days < cfg.new_merchant_min_history_days:
                continue
            if abs(txn.amount_minor) < cfg.new_merchant_min_amount_minor:
                continue
            lookback_start = txn.txn_date - timedelta(days=cfg.new_merchant_lookback_days)
            if any(p.txn_date >= lookback_start for p in members[:index]):
                continue
            found.append(
                AlertCandidate(
                    A5,
                    txn.id,
                    (),
                    {
                        "merchant_key": merchant,
                        "currency": currency,
                        "amount_minor": abs(txn.amount_minor),
                        "history_days": (txn.txn_date - earliest).days,
                        "lookback_days": cfg.new_merchant_lookback_days,
                    },
                    f"A5:{merchant}:{currency}:{txn.txn_date.isoformat()}",
                )
            )
    note = None
    if history_days < cfg.new_merchant_min_history_days:
        note = (
            f"needs at least {cfg.new_merchant_min_history_days} days of history; "
            f"have {history_days}"
        )
    return found, note


# ------------------------------------------------------------------------------------ A6
def detect_negative_balances(
    ctx: DetectionContext, cfg: AnomalyConfig
) -> tuple[list[AlertCandidate], str | None]:
    by_account: dict[str, list[CanonicalTransaction]] = defaultdict(list)
    for txn in ctx.txns:
        if (
            txn.reported_balance_minor is not None
            and ctx.account_types.get(txn.account_id) == AccountType.DEPOSIT
        ):
            by_account[txn.account_id].append(txn)
    found: list[AlertCandidate] = []
    enough_anywhere = False
    for rows in by_account.values():
        rows.sort(key=lambda t: (t.txn_date, source_order_key(t), t.id))
        if len(rows) > cfg.negative_min_history:
            enough_anywhere = True
        for index, txn in enumerate(rows):
            balance = txn.reported_balance_minor
            assert balance is not None
            if balance >= -cfg.overdraft_limit_minor or not _recent(txn.txn_date, ctx, cfg):
                continue
            earlier = rows[:index]
            if len(earlier) < cfg.negative_min_history:
                continue
            earlier_balances = [r.reported_balance_minor for r in earlier]
            if any(b is not None and b < -cfg.overdraft_limit_minor for b in earlier_balances):
                continue  # the account has gone negative before: not unexpected any more
            found.append(
                AlertCandidate(
                    A6,
                    txn.id,
                    (),
                    {
                        "balance_minor": balance,
                        "currency": txn.currency,
                        "history_rows": len(earlier),
                        "lowest_earlier_minor": min(b for b in earlier_balances if b is not None),
                        "overdraft_limit_minor": cfg.overdraft_limit_minor,
                    },
                    f"A6:{txn.id}",
                )
            )
    note = (
        None
        if enough_anywhere or found or not by_account
        else (f"needs at least {cfg.negative_min_history} earlier balances on a deposit account")
    )
    return found, note


DETECTORS = (
    (A1, detect_large_amounts),
    (A2, detect_frequency_spikes),
    (A3, detect_duplicate_charges),
    (A4, detect_recurring_increases),
    (A5, detect_new_merchants),
    (A6, detect_negative_balances),
)


def describe_alert(alert: AnomalyAlert) -> str:
    """Plain, neutral wording that states the rule and the comparison. Never claims fraud."""
    d = alert.details
    cur = d.get("currency", "")

    def fmt(minor: int) -> str:
        return Money(int(minor), cur).format() if cur else str(minor)

    tail = " This may be legitimate."
    if alert.rule_id == A1:
        ratio = d["amount_minor"] / d["median_minor"] if d["median_minor"] else 0
        return (
            f"Rule A1 (large amount): this payment of {fmt(d['amount_minor'])} is about "
            f"{ratio:.1f} times the usual {fmt(d['median_minor'])} for the {d['group_kind']} "
            f"'{d['group_label']}', based on {d['baseline_count']} earlier transactions in the "
            f"previous {d['window_days']} days (flagged above {fmt(d['threshold_minor'])})." + tail
        )
    if alert.rule_id == A2:
        return (
            f"Rule A2 (frequency spike): {d['count']} charges from '{d['merchant_key']}' in the "
            f"{d['window_days']} days from {d['window_start']}, against an earlier average of "
            f"{d['prior_mean']} per {d['window_days']} days over {d['prior_windows']} periods."
            + tail
        )
    if alert.rule_id == A3:
        same = "from the same import" if d["same_import"] else "from different imports"
        return (
            f"Rule A3 (possible duplicate charge): two charges of {fmt(d['amount_minor'])} with the "
            f"same description {d['days_apart']} day(s) apart, {same}. Both are kept; check whether "
            "you were charged twice." + tail
        )
    if alert.rule_id == A4:
        return (
            f"Rule A4 (recurring payment increase): '{d['merchant_key']}' ({d['cadence']}) rose from "
            f"{fmt(d['previous_typical_minor'])} to {fmt(d['latest_minor'])}, "
            f"{d['change_pct']}% more (alert threshold {d['threshold_pct']}%)." + tail
        )
    if alert.rule_id == A5:
        return (
            f"Rule A5 (new merchant): first payment to '{d['merchant_key']}' in "
            f"{d['lookback_days']} days, {fmt(d['amount_minor'])}." + tail
        )
    if alert.rule_id == A6:
        return (
            f"Rule A6 (negative balance): the statement shows {fmt(d['balance_minor'])}, "
            f"below the lowest earlier balance of {fmt(d['lowest_earlier_minor'])} over "
            f"{d['history_rows']} earlier rows." + tail
        )
    return f"Rule {alert.rule_id} flagged this item." + tail


@dataclass(slots=True)
class AnomalyReport:
    as_of: date
    created: dict[str, int] = field(default_factory=dict)
    already_known: int = 0
    skipped: dict[str, str] = field(default_factory=dict)  # rule -> why it stayed silent

    @property
    def total_created(self) -> int:
        return sum(self.created.values())


class AnomalyService:
    """Runs the rules, stores new alerts, and records the user's decisions."""

    def __init__(self, conn: sqlite3.Connection, config: AnomalyConfig | None = None) -> None:
        self._conn = conn
        self.config = config or AnomalyConfig()
        self._alerts = AlertRepository(conn)

    def run(self, *, as_of: date | None = None) -> AnomalyReport:
        """Detect new alerts. Existing alerts, in any status, are never recreated."""
        txns = TransactionRepository(self._conn).fetch_all(TransactionFilter())
        categories = {c.id: c for c in CategoryRepository(self._conn).list(active_only=False)}
        accounts = {
            a.id: a.account_type for a in AccountRepository(self._conn).list(active_only=False)
        }
        report = AnomalyReport(as_of=as_of or date.min)
        if not txns:
            report.skipped = {rule: "no transactions" for rule, _ in DETECTORS}
            return report
        report.as_of = as_of or max(t.txn_date for t in txns)
        ctx = DetectionContext(
            classified=classify(txns, categories, AnalyticsConfig()),
            categories=categories,
            account_types=accounts,
            as_of=report.as_of,
            dismissed_recurring=frozenset(dismissed_keys(self._conn)),
        )
        with transaction(self._conn):
            for rule_id, detector in DETECTORS:
                candidates, note = detector(ctx, self.config)
                if note and not candidates:
                    report.skipped[rule_id] = note
                for candidate in candidates:
                    alert = self._alerts.create(
                        rule_id=candidate.rule_id,
                        details=candidate.details,
                        dedupe_key=candidate.dedupe_key,
                        transaction_id=candidate.transaction_id,
                        related_ids=candidate.related_ids,
                    )
                    if alert is None:
                        report.already_known += 1
                    else:
                        report.created[rule_id] = report.created.get(rule_id, 0) + 1
        return report

    def open_alerts(self) -> list[AnomalyAlert]:
        return self._alerts.list_alerts(status=AlertStatus.OPEN)

    def dismiss(self, alert_id: str) -> AnomalyAlert:
        """The user says this is fine. Kept in history; not raised again."""
        return self._alerts.resolve(alert_id, AlertStatus.DISMISSED)

    def confirm(self, alert_id: str) -> AnomalyAlert:
        """The user agrees this deserves attention. Recorded only: no action is ever taken."""
        return self._alerts.resolve(alert_id, AlertStatus.CONFIRMED)
