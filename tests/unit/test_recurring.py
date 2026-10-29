from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from fractions import Fraction

from analytics_data import make, money
from bank_analyzer.database.repositories import RecurringRepository
from bank_analyzer.domain.models import RecurringStatus
from bank_analyzer.services.recurring import (
    amount_changes,
    detect_recurring,
    dismissed_keys,
    sync_recurring,
)

# Monthly dates with realistic 28-31 day gaps.
MONTHLY = ["2024-01-05", "2024-02-05", "2024-03-05", "2024-04-05", "2024-05-05", "2024-06-05"]


def series(desc: str, amount: str, dates: list[str], **kwargs):  # noqa: ANN003, ANN201
    return [make(d, desc, amount, **kwargs) for d in dates]


class TestDetection:
    def test_monthly_subscription(self) -> None:
        (c,) = detect_recurring(series("NETFLIX", "-15.99", MONTHLY))
        assert (c.merchant_key, c.cadence_label, c.cadence_days) == ("NETFLIX", "monthly", 30)
        assert (c.typical_amount_minor, c.last_amount_minor) == (-1599, -1599)
        assert c.occurrences == 6 and c.amount_stable and c.direction == "out"
        assert (c.first_date, c.last_date) == (date(2024, 1, 5), date(2024, 6, 5))
        assert len(c.transaction_ids) == 6

    def test_other_cadences(self) -> None:
        start = date(2024, 1, 1)
        for gap, label in ((7, "weekly"), (14, "fortnightly"), (91, "quarterly"), (365, "yearly")):
            dates = [(start + timedelta(days=gap * i)).isoformat() for i in range(4)]
            (c,) = detect_recurring(series("GYM", "-10.00", dates))
            assert c.cadence_label == label, label

    def test_irregular_or_too_few_are_not_recurring(self) -> None:
        irregular = ["2024-01-03", "2024-01-20", "2024-03-11", "2024-03-14", "2024-06-30"]
        assert detect_recurring(series("SHOP", "-10.00", irregular)) == []
        assert detect_recurring(series("NETFLIX", "-15.99", MONTHLY[:2])) == []

    def test_one_missed_month_is_tolerated(self) -> None:
        dates = ["2024-01-05", "2024-02-05", "2024-03-05", "2024-04-05", "2024-05-05", "2024-07-05"]
        (c,) = detect_recurring(series("NETFLIX", "-15.99", dates))  # 5 of 6 gaps fit (83%)
        assert c.cadence_label == "monthly"

    def test_variable_amounts_are_recurring_but_not_stable(self) -> None:
        amounts = ["-60.00", "-120.00", "-80.00", "-140.00", "-70.00", "-100.00"]
        rows = [make(d, "POWER CO", a) for d, a in zip(MONTHLY, amounts, strict=True)]
        (c,) = detect_recurring(rows)
        assert c.cadence_label == "monthly" and not c.amount_stable

    def test_two_charges_on_one_day_count_as_one_occurrence(self) -> None:
        rows = series("NETFLIX", "-15.99", MONTHLY)
        rows.append(make("2024-03-05", "NETFLIX", "-15.99"))  # double charge
        (c,) = detect_recurring(rows)
        assert c.occurrences == 6 and money("-31.98") in c.amounts
        assert len(c.transaction_ids) == 7

    def test_money_in_and_out_from_one_merchant_are_separate(self) -> None:
        rows = series("ACME", "3000.00", MONTHLY) + series("ACME", "-15.00", MONTHLY)
        assert sorted(c.direction for c in detect_recurring(rows)) == ["in", "out"]

    def test_currencies_are_separate(self) -> None:
        rows = series("ACME", "-5.00", MONTHLY) + series("ACME", "-5.00", MONTHLY, currency="EUR")
        assert sorted(c.currency for c in detect_recurring(rows)) == ["EUR", "USD"]

    def test_transfers_voided_and_keyless_rows_are_ignored(self) -> None:
        assert detect_recurring(series("ACME", "-5.00", MONTHLY, group="g")) == []
        assert detect_recurring(series("ACME", "-5.00", MONTHLY, voided=True)) == []
        assert detect_recurring(series("12345678", "-5.00", MONTHLY)) == []  # no merchant key

    def test_salary_is_found_as_recurring_income(self) -> None:
        (c,) = detect_recurring(series("SALARY ACME CORP", "3000.00", MONTHLY, category="Salary"))
        assert c.direction == "in" and c.typical_amount_minor == money("3000.00")


class TestAmountChanges:
    def history(self, latest: str, n: int = 5):  # noqa: ANN202
        rows = series("NETFLIX", "-15.99", MONTHLY[:n])
        rows.append(make(MONTHLY[n] if n < len(MONTHLY) else "2024-07-05", "NETFLIX", latest))
        return detect_recurring(rows)

    def test_a_price_rise_over_the_threshold_is_flagged(self) -> None:
        (change,) = amount_changes(self.history("-17.99"), threshold_pct=10)
        assert change.previous_typical_minor == -1599 and change.latest_minor == -1799
        assert change.change_pct == Fraction(200, 1599)  # exactly 12.5078...%

    def test_a_small_rise_is_not_flagged(self) -> None:
        assert amount_changes(self.history("-16.49"), threshold_pct=10) == []  # 3.1%

    def test_a_price_drop_is_ignored_by_default_and_reported_on_request(self) -> None:
        history = self.history("-12.99")
        assert amount_changes(history, threshold_pct=10) == []
        assert len(amount_changes(history, threshold_pct=10, increases_only=False)) == 1

    def test_a_naturally_variable_bill_is_never_called_a_price_change(self) -> None:
        amounts = ["-60.00", "-120.00", "-80.00", "-140.00", "-70.00", "-300.00"]
        rows = [make(d, "POWER CO", a) for d, a in zip(MONTHLY, amounts, strict=True)]
        assert amount_changes(detect_recurring(rows)) == []

    def test_needs_enough_earlier_payments(self) -> None:
        rows = series("NETFLIX", "-15.99", MONTHLY[:2]) + [make("2024-03-05", "NETFLIX", "-25.99")]
        assert amount_changes(detect_recurring(rows), min_previous=3) == []

    def test_a_larger_incoming_amount_counts_as_an_increase_by_size(self) -> None:
        rows = series("ACME", "3000.00", MONTHLY[:5]) + [make("2024-06-05", "ACME", "3600.00")]
        (change,) = amount_changes(detect_recurring(rows))
        assert change.change_pct == Fraction(1, 5)


class TestPersistence:
    def candidates(self):  # noqa: ANN202
        return detect_recurring(
            series("NETFLIX", "-15.99", MONTHLY) + series("ACME", "3000.00", MONTHLY)
        )

    def test_sync_stores_and_refreshes(self, conn: sqlite3.Connection) -> None:
        records = sync_recurring(conn, self.candidates())
        assert sorted(r.merchant_key for r in records) == ["ACME", "NETFLIX"]
        assert all(r.status == RecurringStatus.CANDIDATE for r in records)
        again = sync_recurring(conn, self.candidates())
        assert [r.id for r in sorted(again, key=lambda r: r.merchant_key)] == [
            r.id for r in sorted(records, key=lambda r: r.merchant_key)
        ]
        assert len(RecurringRepository(conn).list_records()) == 2  # no duplicates

    def test_the_users_choice_survives_re_detection(self, conn: sqlite3.Connection) -> None:
        netflix = next(
            r for r in sync_recurring(conn, self.candidates()) if r.merchant_key == "NETFLIX"
        )
        repo = RecurringRepository(conn)
        repo.set_status(netflix.id, RecurringStatus.DISMISSED)
        refreshed = sync_recurring(conn, self.candidates())
        assert (
            next(r for r in refreshed if r.merchant_key == "NETFLIX").status
            == RecurringStatus.DISMISSED
        )
        assert dismissed_keys(conn) == {("NETFLIX", "USD")}
        assert [r.merchant_key for r in repo.list_records(status=RecurringStatus.CANDIDATE)] == [
            "ACME"
        ]

    def test_income_and_payments_to_one_merchant_are_stored_separately(
        self, conn: sqlite3.Connection
    ) -> None:
        both = detect_recurring(
            series("ACME", "3000.00", MONTHLY) + series("ACME", "-15.00", MONTHLY)
        )
        sync_recurring(conn, both)
        assert len(RecurringRepository(conn).list_records()) == 2
