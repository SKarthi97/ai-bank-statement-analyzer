"""Recurring payments (subscriptions, rent, salary) detected from the transaction history.

A merchant is a recurring *candidate* when it appears at least ``min_occurrences`` times, in one
direction and currency, with gaps that mostly match a known cadence (weekly, fortnightly, monthly,
quarterly, yearly) within a tolerance. Detection proposes; the user confirms or dismisses, and that
choice survives re-detection. Variable amounts (a utility bill) are still recurring but flagged
as not stable, so only stable amounts feed price-change alerts.

Same-day charges from one merchant are combined into one occurrence so a double charge does not
look like a zero-day interval.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from bank_analyzer.database.repositories import RecurringRepository
from bank_analyzer.domain.models import CanonicalTransaction, RecurringRecord, RecurringStatus


@dataclass(frozen=True, slots=True)
class RecurringConfig:
    min_occurrences: int = 3
    interval_fraction: float = 0.8  # share of gaps that must fit the cadence
    amount_tolerance_pct: int = 25  # amounts within this % of the median count as stable
    # (days, label, tolerance in days)
    cadences: tuple[tuple[int, str, int], ...] = (
        (7, "weekly", 1),
        (14, "fortnightly", 2),
        (30, "monthly", 4),
        (91, "quarterly", 7),
        (365, "yearly", 10),
    )


@dataclass(frozen=True, slots=True)
class RecurringCandidate:
    merchant_key: str
    currency: str
    cadence_days: int
    cadence_label: str
    typical_amount_minor: int  # signed median (negative = money out)
    last_amount_minor: int
    occurrences: int
    first_date: date
    last_date: date
    amount_stable: bool
    amounts: tuple[int, ...]  # chronological, one per occurrence
    transaction_ids: tuple[str, ...]

    @property
    def direction(self) -> str:
        return "in" if self.typical_amount_minor > 0 else "out"


@dataclass(frozen=True, slots=True)
class AmountChange:
    candidate: RecurringCandidate
    previous_typical_minor: int
    latest_minor: int
    change_pct: Fraction  # signed, relative to the previous typical amount's size


def _median(values: Sequence[int]) -> Fraction:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[mid])
    return Fraction(ordered[mid - 1] + ordered[mid], 2)


def detect_recurring(
    txns: Sequence[CanonicalTransaction], config: RecurringConfig | None = None
) -> list[RecurringCandidate]:
    """Find recurring candidates among live transactions (matched transfers are ignored)."""
    cfg = config or RecurringConfig()
    groups: dict[tuple[str, str, bool], list[CanonicalTransaction]] = {}
    for txn in txns:
        if (
            txn.is_voided
            or txn.amount_minor == 0
            or txn.merchant_key is None
            or txn.transfer_group_id is not None
        ):
            continue
        groups.setdefault((txn.merchant_key, txn.currency, txn.amount_minor > 0), []).append(txn)

    found: list[RecurringCandidate] = []
    for (merchant, currency, _), members in groups.items():
        by_date: dict[date, list[CanonicalTransaction]] = {}
        for txn in members:
            by_date.setdefault(txn.txn_date, []).append(txn)
        dates = sorted(by_date)
        if len(dates) < cfg.min_occurrences:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
        median_gap = _median(gaps)
        match = next(
            (c for c in cfg.cadences if abs(median_gap - c[0]) <= c[2]),
            None,
        )
        if match is None:
            continue
        days, label, tolerance = match
        fitting = sum(1 for g in gaps if abs(g - days) <= tolerance)
        if fitting / len(gaps) < cfg.interval_fraction:
            continue
        amounts = tuple(sum(t.amount_minor for t in by_date[d]) for d in dates)
        typical = _median(amounts)
        limit = abs(typical) * cfg.amount_tolerance_pct / 100
        stable = all(abs(a - typical) <= limit for a in amounts)
        found.append(
            RecurringCandidate(
                merchant_key=merchant,
                currency=currency,
                cadence_days=days,
                cadence_label=label,
                typical_amount_minor=round(typical),
                last_amount_minor=amounts[-1],
                occurrences=len(dates),
                first_date=dates[0],
                last_date=dates[-1],
                amount_stable=stable,
                amounts=amounts,
                transaction_ids=tuple(t.id for d in dates for t in by_date[d]),
            )
        )
    return sorted(found, key=lambda c: (c.merchant_key, c.currency, c.typical_amount_minor))


def amount_changes(
    candidates: Sequence[RecurringCandidate],
    *,
    threshold_pct: int = 10,
    increases_only: bool = True,
    min_previous: int = 2,
) -> list[AmountChange]:
    """Recurring items whose latest amount differs from their earlier typical amount.

    Only candidates whose earlier amounts were stable are compared (a naturally variable bill is
    not a price change). "Increase" means a bigger payment: a larger outflow or a larger inflow
    is judged by absolute size.
    """
    changes: list[AmountChange] = []
    for candidate in candidates:
        earlier = candidate.amounts[:-1]
        if len(earlier) < min_previous:
            continue
        typical = _median(earlier)
        limit = abs(typical) * 25 / 100
        if typical == 0 or any(abs(a - typical) > limit for a in earlier):
            continue  # earlier amounts were not stable, so a "change" would be noise
        latest = candidate.last_amount_minor
        delta_size = Fraction(abs(latest)) - abs(typical)
        pct = delta_size / abs(typical)
        if increases_only and pct < 0:
            continue
        if abs(pct) * 100 >= threshold_pct:
            changes.append(AmountChange(candidate, round(typical), latest, pct))
    return changes


def sync_recurring(
    conn: sqlite3.Connection, candidates: Sequence[RecurringCandidate]
) -> list[RecurringRecord]:
    """Store candidates. A status the user set (confirmed/dismissed) is kept on refresh."""
    repo = RecurringRepository(conn)
    return [
        repo.upsert(
            merchant_key=c.merchant_key,
            currency=c.currency,
            cadence_days=c.cadence_days,
            typical_amount_minor=c.typical_amount_minor,
            last_amount_minor=c.last_amount_minor,
            occurrences=c.occurrences,
            first_date=c.first_date,
            last_date=c.last_date,
        )
        for c in candidates
    ]


def dismissed_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(merchant_key, currency) the user dismissed, to be left out of alerts."""
    return {
        (r.merchant_key, r.currency)
        for r in RecurringRepository(conn).list_records(status=RecurringStatus.DISMISSED)
    }
