"""Anomaly rules checked against hand-computed baselines.

A1 worked example (amounts in dollars): earlier payments 40,45,50,55,60,40,45,50,55,60 ->
median 50; absolute deviations 10,5,0,5,10,10,5,0,5,10 -> MAD 5; scaled MAD 5 x 1.4826 = 7.413;
k = 5 -> 50 + 37.065 = 87.065; 95th percentile (nearest rank, n = 10) = 60. Threshold =
max(87.065, 60) = 87.065. So 90.00 is flagged and 85.00 is not.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from analytics_data import BY_ID, make, money
from bank_analyzer.domain.models import AccountType, AnomalyAlert
from bank_analyzer.services.analytics import AnalyticsConfig, classify
from bank_analyzer.services.anomaly_detection import (
    AnomalyConfig,
    DetectionContext,
    describe_alert,
    detect_duplicate_charges,
    detect_frequency_spikes,
    detect_large_amounts,
    detect_negative_balances,
    detect_new_merchants,
    detect_recurring_increases,
)

AS_OF = date(2024, 6, 30)
CFG = AnomalyConfig()


def ctx(txns, *, as_of: date = AS_OF, dismissed=frozenset()) -> DetectionContext:  # noqa: ANN001
    return DetectionContext(
        classified=classify(list(txns), BY_ID, AnalyticsConfig()),
        categories=BY_ID,
        account_types={"A": AccountType.DEPOSIT, "C": AccountType.CREDIT_CARD},
        as_of=as_of,
        dismissed_recurring=dismissed,
    )


def days_before(n: int, base: date = AS_OF) -> str:
    return (base - timedelta(days=n)).isoformat()


def groceries(amounts: list[str], *, first_offset: int = 120, step: int = 7, **kwargs):  # noqa: ANN003, ANN201
    """Earlier payments, oldest first, one every ``step`` days."""
    return [
        make(days_before(first_offset - step * i), "GROCERY MART", a, "Groceries", **kwargs)
        for i, a in enumerate(amounts)
    ]


class TestA1LargeAmount:
    FLAT = ["-50.00"] * 8

    @pytest.mark.parametrize(
        ("amount", "flagged"),
        [
            ("-400.00", True),
            ("-75.00", True),
            ("-74.99", False),
            ("-52.00", False),
            ("-50.00", False),
        ],
    )
    def test_flat_history_needs_both_a_high_threshold_and_a_real_jump(
        self, amount: str, flagged: bool
    ) -> None:
        target = make(AS_OF.isoformat(), "GROCERY MART", amount, "Groceries")
        found, _ = detect_large_amounts(ctx([*groceries(self.FLAT), target]), CFG)
        assert [c.transaction_id for c in found] == ([target.id] if flagged else [])

    def test_hand_computed_baseline_and_threshold(self) -> None:
        history = ["-40.00", "-45.00", "-50.00", "-55.00", "-60.00"] * 2
        base = groceries(history)
        for amount, flagged in (("-90.00", True), ("-85.00", False)):
            target = make(AS_OF.isoformat(), "GROCERY MART", amount, "Groceries")
            found, _ = detect_large_amounts(ctx([*base, target]), CFG)
            assert bool(found) is flagged, amount
            if flagged:
                d = found[0].details
                assert (d["median_minor"], d["mad_minor"], d["p95_minor"]) == (5000, 500, 6000)
                assert 8706 <= d["threshold_minor"] <= 8707  # 50 + 5 x 7.413
                assert d["baseline_count"] == 10 and d["amount_minor"] == 9000
                assert d["group_kind"] == "category" and d["group_label"] == "Groceries"
                assert len(found[0].related_ids) == 5  # the baseline transactions it compared with

    def test_not_enough_history_stays_silent_and_says_why(self) -> None:
        target = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries")
        found, note = detect_large_amounts(ctx([*groceries(["-50.00"] * 7), target]), CFG)
        assert found == [] and "at least 8 earlier transactions" in (note or "")

    def test_old_history_outside_the_window_does_not_count(self) -> None:
        old = groceries(["-50.00"] * 8, first_offset=400)  # 344-400 days before: outside 180 days
        target = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries")
        assert detect_large_amounts(ctx([*old, target]), CFG)[0] == []

    def test_a_same_day_purchase_is_not_part_of_the_baseline(self) -> None:
        a = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries")
        b = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries")
        found, _ = detect_large_amounts(ctx([*groceries(self.FLAT), a, b]), CFG)
        assert {c.transaction_id for c in found} == {a.id, b.id}  # neither raised the other's bar

    def test_only_recent_transactions_raise_new_alerts(self) -> None:
        old_big = make(days_before(120), "GROCERY MART", "-400.00", "Groceries")
        history = groceries(self.FLAT, first_offset=300)
        assert detect_large_amounts(ctx([*history, old_big]), CFG)[0] == []

    def test_uncategorized_spending_is_compared_per_merchant(self) -> None:
        history = [make(days_before(120 - 7 * i), "CORNER SHOP", "-20.00") for i in range(8)]
        target = make(AS_OF.isoformat(), "CORNER SHOP", "-200.00")
        (alert,) = detect_large_amounts(ctx([*history, target]), CFG)[0]
        assert (
            alert.details["group_kind"] == "merchant"
            and alert.details["group_label"] == "CORNER SHOP"
        )

    def test_money_in_cash_and_transfers_are_never_candidates(self) -> None:
        history = groceries(self.FLAT)
        others = [
            make(AS_OF.isoformat(), "BIG PAY", "5000.00", "Salary"),
            make(AS_OF.isoformat(), "ATM", "-900.00", "Cash withdrawals"),
            make(AS_OF.isoformat(), "TRANSFER", "-900.00", "Groceries", group="g1"),
        ]
        assert detect_large_amounts(ctx([*history, *others]), CFG)[0] == []

    def test_currencies_do_not_mix_in_a_baseline(self) -> None:
        history = groceries(self.FLAT)
        eur = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries", currency="EUR")
        assert detect_large_amounts(ctx([*history, eur]), CFG)[0] == []  # no EUR baseline

    def test_wording_has_a_stable_identity_key(self) -> None:
        target = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries")
        (alert,) = detect_large_amounts(ctx([*groceries(self.FLAT), target]), CFG)[0]
        assert alert.dedupe_key == f"A1:{target.id}" and alert.rule_id == "A1"


class TestA2FrequencySpike:
    CS = AS_OF - timedelta(days=6)  # start of the current 7-day window: 2024-06-24

    def coffee(self, offsets: list[int]):  # noqa: ANN202
        """Coffee purchases ``offset`` days before the start of the current window."""
        return [
            make((self.CS - timedelta(days=o)).isoformat(), "COFFEE BAR", "-4.00") for o in offsets
        ]

    def now(self, n: int):  # noqa: ANN202
        return [
            make((self.CS + timedelta(days=i)).isoformat(), "COFFEE BAR", "-4.00") for i in range(n)
        ]

    def anchor(self):  # noqa: ANN202
        return [make((self.CS - timedelta(days=56)).isoformat(), "ANCHOR SHOP", "-9.00")]

    WEEKLY = [3 + 7 * j for j in range(10)]  # one coffee in each of the ten earlier weeks

    @pytest.mark.parametrize(("count", "flagged"), [(2, False), (3, True), (4, True)])
    def test_steady_weekly_habit_then_a_burst(self, count: int, flagged: bool) -> None:
        found, _ = detect_frequency_spikes(
            ctx([*self.anchor(), *self.coffee(self.WEEKLY), *self.now(count)]), CFG
        )
        assert bool(found) is flagged
        if flagged:
            d = found[0].details
            assert d["count"] == count and d["prior_mean"] == "1.00" and d["prior_std"] == "0.00"

    def test_a_varying_habit_uses_the_standard_deviation(self) -> None:
        # Eight earlier weeks with 1,2,1,2,1,2,1,2 purchases: mean 1.5, variance 0.25, std 0.5.
        # Week j (0 = the week just before the current one) gets a purchase at 7j+3 days back,
        # and odd weeks a second one at 7j+4. A burst must beat 1.5 + 3 x 0.5 = 3.0.
        offsets = [7 * j + 3 for j in range(8)] + [7 * j + 4 for j in (1, 3, 5, 7)]
        history = [*self.anchor(), *self.coffee(offsets)]
        assert detect_frequency_spikes(ctx([*history, *self.now(3)]), CFG)[0] == []
        found, _ = detect_frequency_spikes(ctx([*history, *self.now(4)]), CFG)
        assert len(found) == 1
        assert (found[0].details["prior_mean"], found[0].details["prior_std"]) == ("1.50", "0.50")

    def test_window_edges_regression(self) -> None:
        """A purchase exactly 7 days before the current window belongs to the window just before
        it. Eight purchases at 7, 8, 15, 22, ... days back are one per earlier week (std 0)."""
        offsets = [7, 8, 15, 22, 29, 36, 43, 50]
        found, _ = detect_frequency_spikes(
            ctx([*self.anchor(), *self.coffee(offsets), *self.now(4)]), CFG
        )
        assert found and found[0].details["prior_std"] == "0.00"

    def test_a_merchant_seen_in_few_earlier_weeks_is_not_a_frequency_spike(self) -> None:
        history = [*self.anchor(), *self.coffee([3])]
        assert detect_frequency_spikes(ctx([*history, *self.now(5)]), CFG)[0] == []

    def test_short_history_stays_silent_and_says_why(self) -> None:
        recent = [make(days_before(10), "COFFEE BAR", "-4.00"), *self.now(5)]
        found, note = detect_frequency_spikes(ctx(recent), CFG)
        assert found == [] and "at least 56 days" in (note or "")

    def test_no_transactions(self) -> None:
        assert detect_frequency_spikes(ctx([]), CFG) == ([], "no transactions")

    def test_one_alert_per_merchant_per_window(self) -> None:
        found, _ = detect_frequency_spikes(
            ctx([*self.anchor(), *self.coffee(self.WEEKLY), *self.now(4)]), CFG
        )
        assert found[0].dedupe_key == f"A2:COFFEE BAR:USD:{self.CS.isoformat()}"


class TestA3DuplicateCharge:
    def test_two_identical_charges_a_day_apart(self) -> None:
        a = make(days_before(5), "CITY GYM", "-29.99")
        b = make(days_before(4), "CITY GYM", "-29.99")
        (alert,) = detect_duplicate_charges(ctx([a, b]), CFG)[0]
        assert (alert.transaction_id, alert.related_ids) == (b.id, (a.id,))
        assert alert.details["days_apart"] == 1 and alert.details["amount_minor"] == 2999

    def test_a_long_daily_habit_is_not_a_duplicate(self) -> None:
        habit = [make(days_before(10 - d), "MORNING COFFEE", "-3.50") for d in range(6)]
        assert detect_duplicate_charges(ctx(habit), CFG)[0] == []

    def test_a_run_of_three_still_alerts_each_neighbouring_pair(self) -> None:
        run = [make(days_before(10 - d), "SNACK", "-3.50") for d in range(3)]
        assert len(detect_duplicate_charges(ctx(run), CFG)[0]) == 2

    def test_old_pairs_do_not_raise_new_alerts(self) -> None:
        old = [
            make(days_before(200), "CITY GYM", "-29.99"),
            make(days_before(199), "CITY GYM", "-29.99"),
        ]
        assert detect_duplicate_charges(ctx(old), CFG)[0] == []

    def test_money_in_and_transfers_are_ignored(self) -> None:
        pay = [
            make(days_before(5), "REFUND", "20.00", "Refunds"),
            make(days_before(4), "REFUND", "20.00", "Refunds"),
        ]
        xfer = [
            make(days_before(5), "MOVE", "-20.00", group="g"),
            make(days_before(4), "MOVE", "-20.00", group="g"),
        ]
        assert detect_duplicate_charges(ctx([*pay, *xfer]), CFG)[0] == []

    def test_minimum_amount_filter(self) -> None:
        pair = [make(days_before(5), "TIP JAR", "-1.00"), make(days_before(4), "TIP JAR", "-1.00")]
        cfg = AnomalyConfig(duplicate_min_amount_minor=500)
        assert detect_duplicate_charges(ctx(pair), cfg)[0] == []
        assert len(detect_duplicate_charges(ctx(pair), CFG)[0]) == 1


class TestA4RecurringIncrease:
    MONTHLY = ["2024-01-30", "2024-02-29", "2024-03-30", "2024-04-30", "2024-05-30"]

    def history(self, latest: str):  # noqa: ANN202
        rows = [make(d, "NETFLIX", "-15.99", "Subscriptions") for d in self.MONTHLY]
        rows.append(make("2024-06-29", "NETFLIX", latest, "Subscriptions"))
        return rows

    def test_price_rise_is_flagged_with_the_comparison(self) -> None:
        (alert,) = detect_recurring_increases(ctx(self.history("-17.99")), CFG)[0]
        d = alert.details
        assert (d["previous_typical_minor"], d["latest_minor"], d["cadence"]) == (
            1599,
            1799,
            "monthly",
        )
        assert d["change_pct"] == "12.5" and d["threshold_pct"] == 10

    def test_small_change_and_price_drop_are_not_flagged(self) -> None:
        assert detect_recurring_increases(ctx(self.history("-16.49")), CFG)[0] == []
        assert detect_recurring_increases(ctx(self.history("-12.99")), CFG)[0] == []

    def test_a_dismissed_subscription_is_left_alone(self) -> None:
        dismissed = frozenset({("NETFLIX", "USD")})
        assert (
            detect_recurring_increases(ctx(self.history("-17.99"), dismissed=dismissed), CFG)[0]
            == []
        )

    def test_a_subscription_that_stopped_long_ago_is_not_news(self) -> None:
        assert (
            detect_recurring_increases(ctx(self.history("-17.99"), as_of=date(2025, 1, 1)), CFG)[0]
            == []
        )


class TestA5NewMerchant:
    def base(self, start: str = "2024-01-05"):  # noqa: ANN202
        """Five months of ordinary spending so there is history to compare against."""
        d0 = date.fromisoformat(start)
        rows = []
        for m in range(6):
            day = (d0 + timedelta(days=30 * m)).isoformat()
            rows.append(make(day, "RENT", "-900.00", "Housing"))
            rows.append(make(day, "GROCERY MART", "-80.00", "Groceries"))
        return rows

    def test_first_sizeable_payment_to_an_unseen_merchant(self) -> None:
        new = make("2024-06-20", "NEW SHOP", "-60.00")
        (alert,) = detect_new_merchants(ctx([*self.base(), new]), CFG)[0]
        assert alert.transaction_id == new.id
        assert alert.details["merchant_key"] == "NEW SHOP" and alert.details["amount_minor"] == 6000
        assert alert.details["history_days"] >= 90

    def test_a_second_payment_to_the_same_merchant_is_not_new(self) -> None:
        rows = [
            *self.base(),
            make("2024-06-20", "NEW SHOP", "-60.00"),
            make("2024-06-25", "NEW SHOP", "-60.00"),
        ]
        assert len(detect_new_merchants(ctx(rows), CFG)[0]) == 1

    def test_small_payments_are_ignored(self) -> None:
        assert (
            detect_new_merchants(
                ctx([*self.base(), make("2024-06-20", "NEW SHOP", "-10.00")]), CFG
            )[0]
            == []
        )

    def test_short_history_stays_silent_and_says_why(self) -> None:
        rows = [make("2024-05-01", "RENT", "-900.00"), make("2024-06-20", "NEW SHOP", "-60.00")]
        found, note = detect_new_merchants(ctx(rows), CFG)
        assert found == [] and "at least 90 days" in (note or "")

    def test_a_merchant_seen_within_the_lookback_is_not_new(self) -> None:
        rows = [
            *self.base(),
            make("2024-03-01", "NEW SHOP", "-60.00"),
            make("2024-06-20", "NEW SHOP", "-60.00"),
        ]
        assert detect_new_merchants(ctx(rows), CFG)[0] == []

    def test_a_merchant_last_seen_long_ago_counts_as_new_again(self) -> None:
        rows = [
            *self.base(),
            make("2022-01-01", "NEW SHOP", "-60.00"),
            make("2024-06-20", "NEW SHOP", "-60.00"),
        ]
        assert len(detect_new_merchants(ctx(rows), CFG)[0]) == 1

    def test_only_recent_first_payments_alert(self) -> None:
        rows = [*self.base(), make("2024-03-01", "NEW SHOP", "-60.00")]  # first seen 4 months ago
        assert detect_new_merchants(ctx(rows), CFG)[0] == []

    def test_the_first_import_does_not_flood_with_everything_new(self) -> None:
        d0 = date(2024, 5, 1)
        rows = [
            make((d0 + timedelta(days=i)).isoformat(), f"SHOP {i}", "-50.00") for i in range(60)
        ]
        assert detect_new_merchants(ctx(rows), CFG)[0] == []


class TestA6NegativeBalance:
    def deposit(self, balances: list[str], *, account: str = "A", start_offset: int = 60):  # noqa: ANN202
        return [
            make(
                days_before(start_offset - 4 * i),
                f"ITEM {i}",
                "-1.00",
                account=account,
                row=i + 1,
                balance=b,
            )
            for i, b in enumerate(balances)
        ]

    HEALTHY = ["500.00"] * 12

    def test_first_dip_below_zero_after_a_healthy_history(self) -> None:
        rows = self.deposit([*self.HEALTHY, "-50.00"])
        (alert,) = detect_negative_balances(ctx(rows), CFG)[0]
        d = alert.details
        assert (
            d["balance_minor"] == -5000
            and d["lowest_earlier_minor"] == 50000
            and d["history_rows"] == 12
        )

    def test_zero_is_not_negative_and_the_overdraft_limit_is_respected(self) -> None:
        assert detect_negative_balances(ctx(self.deposit([*self.HEALTHY, "0.00"])), CFG)[0] == []
        rows = self.deposit([*self.HEALTHY, "-50.00"])
        assert (
            detect_negative_balances(
                ctx(rows), AnomalyConfig(overdraft_limit_minor=money("100.00"))
            )[0]
            == []
        )

    def test_an_account_that_was_negative_before_is_not_surprising(self) -> None:
        rows = self.deposit([*self.HEALTHY[:6], "-10.00", *self.HEALTHY[:6], "-20.00"])
        assert len(detect_negative_balances(ctx(rows), CFG)[0]) == 0

    def test_credit_cards_are_not_checked(self) -> None:
        rows = self.deposit([*self.HEALTHY, "-50.00"], account="C")
        assert detect_negative_balances(ctx(rows), CFG)[0] == []

    def test_too_little_history_stays_silent(self) -> None:
        found, note = detect_negative_balances(ctx(self.deposit(["500.00"] * 4 + ["-50.00"])), CFG)
        assert found == [] and "at least 10 earlier balances" in (note or "")

    def test_old_dips_do_not_raise_new_alerts(self) -> None:
        rows = self.deposit([*self.HEALTHY, "-50.00"], start_offset=400)
        assert detect_negative_balances(ctx(rows), CFG)[0] == []

    def test_rows_without_a_balance_are_skipped(self) -> None:
        rows = [make(days_before(10), "X", "-1.00")]
        assert detect_negative_balances(ctx(rows), CFG)[0] == []


FORBIDDEN_WORDS = ("fraud", "stolen", "steal", "scam", "hack", "criminal", "theft", "crime")


class TestWording:
    """Alerts state a rule and a comparison. They never accuse anyone of anything."""

    def alert_for(self, candidate):  # noqa: ANN001, ANN202
        return AnomalyAlert(
            id="x",
            rule_id=candidate.rule_id,
            transaction_id=candidate.transaction_id,
            related_transaction_ids=candidate.related_ids,
            details=candidate.details,
            created_at="2024-06-30T00:00:00Z",
        )

    def every_rule(self):  # noqa: ANN202
        flat = groceries(["-50.00"] * 8)
        big = make(AS_OF.isoformat(), "GROCERY MART", "-400.00", "Groceries")
        a1 = detect_large_amounts(ctx([*flat, big]), CFG)[0]

        cs = AS_OF - timedelta(days=6)
        anchor = make((cs - timedelta(days=56)).isoformat(), "ANCHOR SHOP", "-9.00")
        weekly = [
            make((cs - timedelta(days=3 + 7 * j)).isoformat(), "COFFEE BAR", "-4.00")
            for j in range(10)
        ]
        burst = [
            make((cs + timedelta(days=i)).isoformat(), "COFFEE BAR", "-4.00") for i in range(4)
        ]
        a2 = detect_frequency_spikes(ctx([anchor, *weekly, *burst]), CFG)[0]

        twin = [
            make(days_before(5), "CITY GYM", "-29.99"),
            make(days_before(4), "CITY GYM", "-29.99"),
        ]
        a3 = detect_duplicate_charges(ctx(twin), CFG)[0]

        months = ["2024-01-30", "2024-02-29", "2024-03-30", "2024-04-30", "2024-05-30"]
        netflix = [make(d, "NETFLIX", "-15.99", "Subscriptions") for d in months]
        netflix.append(make("2024-06-29", "NETFLIX", "-17.99", "Subscriptions"))
        a4 = detect_recurring_increases(ctx(netflix), CFG)[0]

        base = TestA5NewMerchant().base()
        a5 = detect_new_merchants(ctx([*base, make("2024-06-20", "NEW SHOP", "-60.00")]), CFG)[0]

        deposit = TestA6NegativeBalance().deposit([*TestA6NegativeBalance.HEALTHY, "-50.00"])
        a6 = detect_negative_balances(ctx(deposit), CFG)[0]
        return {"A1": a1, "A2": a2, "A3": a3, "A4": a4, "A5": a5, "A6": a6}

    def test_every_rule_produces_an_alert_in_this_scenario(self) -> None:
        found = self.every_rule()
        assert all(len(v) >= 1 for v in found.values()), {k: len(v) for k, v in found.items()}

    def test_wording_is_neutral_and_names_the_rule(self) -> None:
        for rule, candidates in self.every_rule().items():
            text = describe_alert(self.alert_for(candidates[0]))
            assert text.startswith(f"Rule {rule} ("), text
            assert "may be legitimate" in text
            assert not any(word in text.lower() for word in FORBIDDEN_WORDS), text

    def test_the_text_shows_the_figures_it_compared(self) -> None:
        found = self.every_rule()
        a1 = describe_alert(self.alert_for(found["A1"][0]))
        assert "400.00 USD" in a1 and "50.00 USD" in a1 and "Groceries" in a1 and "8 earlier" in a1
        a4 = describe_alert(self.alert_for(found["A4"][0]))
        assert "15.99 USD" in a4 and "17.99 USD" in a4 and "12.5%" in a4
        a3 = describe_alert(self.alert_for(found["A3"][0]))
        assert "29.99 USD" in a3 and "Both are kept" in a3
        a6 = describe_alert(self.alert_for(found["A6"][0]))
        assert "-50.00 USD" in a6

    def test_an_unknown_rule_still_reads_neutrally(self) -> None:
        alert = AnomalyAlert(id="x", rule_id="A9", details={}, created_at="x")
        assert "may be legitimate" in describe_alert(alert)
