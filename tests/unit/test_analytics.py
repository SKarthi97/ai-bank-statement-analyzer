"""Analytics checked against figures computed by hand from tests/analytics_data.household().

Hand computation, January (USD):
  income            3000.00  (salary only; the 50.00 mystery deposit is uncategorized, not income)
  expenses          1200.00 rent + 85.40 groceries + 60.00 restaurant + 120.00 shop - 120.00 linked
                    refund + 25.00 uncategorized out = 1370.40
  net               3000.00 - 1370.40 = 1629.60
  excluded          cash 200.00; savings 150.00; matched transfers (500 + 300 each way); unmatched 40.00
  raw credits       3000 + 500 + 300 + 120 + 50 = 3970.00
  raw debits        -(1200 + 85.40 + 200 + 500 + 300 + 60 + 120 + 25 + 40 + 150) = -2680.40
February (USD):
  income 3000.00; expenses 1200 + 92.10 + 80 - 45 (unlinked refund) = 1327.10; net 1672.90; cash 100.00
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from analytics_data import BY_ID, CATEGORIES, household, make, money
from bank_analyzer.domain.errors import CurrencyMismatch
from bank_analyzer.domain.models import CategoryRole
from bank_analyzer.domain.money import Money
from bank_analyzer.domain.periods import DateRange
from bank_analyzer.services.analytics import (
    UNCATEGORIZED_NAME,
    UNLINKED_REFUNDS_NAME,
    AnalyticsConfig,
    Analyzer,
    Bucket,
    classify,
)

JAN = DateRange.month("2024-01")
FEB = DateRange.month("2024-02")
JAN_FEB = DateRange(date(2024, 1, 1), date(2024, 2, 29))


@pytest.fixture(scope="module")
def rows():  # noqa: ANN201
    return household()


@pytest.fixture(scope="module")
def analyzer(rows) -> Analyzer:  # noqa: ANN001
    return Analyzer(rows.values(), BY_ID)


def usd(text: str) -> Money:
    return Money(money(text), "USD")


class TestJanuaryOracle:
    def test_summary_buckets(self, analyzer: Analyzer) -> None:
        s = analyzer.summary(JAN, "USD")
        assert s.income == usd("3000.00")
        assert s.expenses == usd("1370.40")
        assert s.net == usd("1629.60")
        assert s.cash_withdrawn == usd("200.00")
        assert s.refunds_netted == usd("120.00")
        assert s.savings_net == usd("-150.00")
        assert s.matched_transfers_net == usd("0.00")
        assert (s.unmatched_transfers_in, s.unmatched_transfers_out) == (usd("0.00"), usd("40.00"))
        assert (s.uncategorized_in, s.uncategorized_out) == (usd("50.00"), usd("25.00"))
        assert (s.raw_credits, s.raw_debits, s.raw_net) == (
            usd("3970.00"),
            usd("-2680.40"),
            usd("1289.60"),
        )
        assert s.transaction_count == 15

    def test_the_bucket_identity_holds(self, analyzer: Analyzer) -> None:
        s = analyzer.summary(JAN, "USD")
        rebuilt = (
            s.net
            - s.cash_withdrawn
            + s.savings_net
            + s.matched_transfers_net
            + (s.unmatched_transfers_in - s.unmatched_transfers_out)
            + s.uncategorized_in
        )
        assert rebuilt == s.raw_net

    def test_metrics_are_the_summary_with_definitions_and_period(self, analyzer: Analyzer) -> None:
        for metric, expected in (
            (analyzer.income(JAN, "USD"), usd("3000.00")),
            (analyzer.expenses(JAN, "USD"), usd("1370.40")),
            (analyzer.net_cash_flow(JAN, "USD"), usd("1629.60")),
            (analyzer.cash_withdrawals(JAN, "USD"), usd("200.00")),
            (analyzer.total_credits(JAN, "USD"), usd("3970.00")),
            (analyzer.total_debits(JAN, "USD"), usd("-2680.40")),
        ):
            assert metric.value == expected and metric.currency == "USD" and metric.period == JAN
            assert len(metric.definition) > 30 and metric.unavailable_reason is None

    def test_every_figure_is_traceable_to_its_transactions(self, analyzer: Analyzer, rows) -> None:  # noqa: ANN001
        by_id = {t.id: t for t in rows.values()}

        def total(metric_ids: tuple[str, ...]) -> int:
            return sum(by_id[i].amount_minor for i in metric_ids)

        assert total(analyzer.income(JAN, "USD").transaction_ids) == money("3000.00")
        assert -total(analyzer.expenses(JAN, "USD").transaction_ids) == money("1370.40")
        assert -total(analyzer.cash_withdrawals(JAN, "USD").transaction_ids) == money("200.00")
        assert total(analyzer.total_credits(JAN, "USD").transaction_ids) == money("3970.00")
        assert total(analyzer.total_debits(JAN, "USD").transaction_ids) == money("-2680.40")
        ids = analyzer.income(JAN, "USD").transaction_ids
        assert ids == (rows["jan_salary"].id,)

    def test_uncategorized_money_is_flagged_not_hidden(self, analyzer: Analyzer) -> None:
        warnings = " ".join(analyzer.income(JAN, "USD").warnings)
        assert "50.00 USD of money in is uncategorized" in warnings
        assert "40.00 USD in total" in warnings  # the unmatched transfer


class TestFebruaryOracle:
    def test_summary(self, analyzer: Analyzer) -> None:
        s = analyzer.summary(FEB, "USD")
        assert (s.income, s.expenses, s.net) == (usd("3000.00"), usd("1327.10"), usd("1672.90"))
        assert s.cash_withdrawn == usd("100.00") and s.refunds_netted == usd("45.00")
        assert (s.raw_credits, s.raw_debits, s.raw_net) == (
            usd("3045.00"),
            usd("-1472.10"),
            usd("1572.90"),
        )
        assert s.transaction_count == 6

    def test_two_month_period_adds_up(self, analyzer: Analyzer) -> None:
        s = analyzer.summary(JAN_FEB, "USD")
        assert s.income == usd("6000.00") and s.expenses == usd("2697.50")
        assert s.net == usd("3302.50")


class TestTransfersRefundsAndCash:
    def test_matched_transfer_legs_never_count_as_income_or_spending(
        self, analyzer: Analyzer
    ) -> None:
        # without matching, the 500 and 300 inflows would be income and the outflows spending
        s = analyzer.summary(JAN, "USD")
        assert s.income == usd("3000.00") and usd("1370.40") == s.expenses

    def test_unmatched_transfer_is_excluded_but_reported(self, analyzer: Analyzer, rows) -> None:  # noqa: ANN001
        s = analyzer.summary(JAN, "USD")
        assert s.ids["transfers_unmatched"] == (rows["jan_friend"].id,)
        assert rows["jan_friend"].id not in s.ids["expenses"]

    def test_linked_refund_nets_against_the_original_category(self, analyzer: Analyzer) -> None:
        spend = {c.name: c for c in analyzer.category_spend(JAN, "USD")}
        assert spend["Shopping"].amount == usd("0.00") and spend["Shopping"].count == 2
        assert UNLINKED_REFUNDS_NAME not in spend

    def test_unlinked_refund_is_shown_separately_and_still_reduces_spending(
        self, analyzer: Analyzer
    ) -> None:
        spend = {c.name: c for c in analyzer.category_spend(FEB, "USD")}
        assert spend[UNLINKED_REFUNDS_NAME].amount == usd("-45.00")
        assert sum(c.amount.minor for c in spend.values()) == money("1327.10")

    def test_category_spend_totals_equal_expenses_and_are_sorted(self, analyzer: Analyzer) -> None:
        spend = analyzer.category_spend(JAN, "USD")
        assert [(c.name, c.amount.to_decimal()) for c in spend] == [
            ("Housing", Decimal("1200.00")),
            ("Groceries", Decimal("85.40")),
            ("Dining", Decimal("60.00")),
            (UNCATEGORIZED_NAME, Decimal("25.00")),
            ("Shopping", Decimal("0.00")),
        ]
        assert sum(c.amount.minor for c in spend) == analyzer.summary(JAN, "USD").expenses.minor

    def test_refund_report(self, analyzer: Analyzer, rows) -> None:  # noqa: ANN001
        jan = analyzer.refunds(JAN, "USD")
        assert jan.linked == ((rows["jan_shop_refund"].id, rows["jan_shop"].id),)
        assert jan.linked_total == usd("120.00") and jan.unlinked_ids == ()
        feb = analyzer.refunds(FEB, "USD")
        assert feb.linked == () and feb.unlinked_total == usd("45.00")

    def test_savings_are_excluded_unless_the_user_opts_in(self, rows) -> None:  # noqa: ANN001
        default = Analyzer(rows.values(), BY_ID).expenses(JAN, "USD").value
        opted_in = Analyzer(rows.values(), BY_ID, AnalyticsConfig(savings_as_expense=True))
        assert default == usd("1370.40")
        assert opted_in.expenses(JAN, "USD").value == usd("1520.40")


class TestRefundLinkingEdgeCases:
    def build(self, *extra):  # noqa: ANN002, ANN202
        return Analyzer(extra, BY_ID)

    def test_refund_in_a_later_month_links_to_the_earlier_purchase(self) -> None:
        buy = make("2024-01-20", "SHOP", "-120.00", "Shopping")
        back = make("2024-02-05", "REFUND SHOP", "120.00", "Refunds")
        a = self.build(buy, back)
        spend = {c.name: c for c in a.category_spend(FEB, "USD")}
        assert spend["Shopping"].amount == usd("-120.00")  # attributed to Shopping, not "unlinked"
        assert a.refunds(FEB, "USD").linked == ((back.id, buy.id),)

    def test_partial_refund_is_not_guessed(self) -> None:
        buy = make("2024-01-20", "SHOP", "-120.00", "Shopping")
        back = make("2024-01-22", "REFUND SHOP", "50.00", "Refunds")
        assert self.build(buy, back).refunds(JAN, "USD").unlinked_ids == (back.id,)

    def test_outside_the_window_stays_unlinked(self) -> None:
        buy = make("2024-01-01", "SHOP", "-120.00", "Shopping")
        back = make("2024-04-30", "REFUND SHOP", "120.00", "Refunds")  # 120 days later
        assert self.build(buy, back).refunds(DateRange.month("2024-04"), "USD").unlinked_ids == (
            back.id,
        )

    def test_other_account_other_merchant_other_currency_do_not_link(self) -> None:
        buy = make("2024-01-20", "SHOP", "-120.00", "Shopping", account="C")
        for back in (
            make("2024-01-22", "REFUND SHOP", "120.00", "Refunds", account="A"),
            make("2024-01-22", "REFUND OTHER", "120.00", "Refunds", account="C"),
            make("2024-01-22", "REFUND SHOP", "120.00", "Refunds", account="C", currency="EUR"),
        ):
            result = self.build(buy, back).refunds(JAN, back.currency)
            assert result.linked == ()

    def test_two_identical_purchases_each_link_once(self) -> None:
        b1 = make("2024-01-10", "SHOP", "-30.00", "Shopping")
        b2 = make("2024-01-15", "SHOP", "-30.00", "Shopping")
        r1 = make("2024-01-20", "REFUND SHOP", "30.00", "Refunds")
        r2 = make("2024-01-21", "REFUND SHOP", "30.00", "Refunds")
        r3 = make("2024-01-22", "REFUND SHOP", "30.00", "Refunds")
        report = self.build(b1, b2, r1, r2, r3).refunds(JAN, "USD")
        assert dict(report.linked) == {
            r1.id: b2.id,
            r2.id: b1.id,
        }  # latest earlier first, once each
        assert report.unlinked_ids == (r3.id,)


class TestSavingsRate:
    def test_january_and_february(self, analyzer: Analyzer) -> None:
        assert analyzer.savings_rate(JAN, "USD").value == Decimal("0.5432")  # 1629.60 / 3000
        assert analyzer.savings_rate(FEB, "USD").value == Decimal("0.5576")  # 1672.90 / 3000
        assert analyzer.savings_rate(JAN_FEB, "USD").value == Decimal("0.5504")  # 3302.50 / 6000

    def test_partial_month_is_unavailable(self, analyzer: Analyzer) -> None:
        r = analyzer.savings_rate(DateRange(date(2024, 1, 1), date(2024, 1, 15)), "USD")
        assert r.value is None and "whole number of calendar months" in (r.unavailable_reason or "")

    def test_no_income_is_unavailable(self) -> None:
        a = Analyzer([make("2024-01-05", "RENT", "-100.00", "Housing")], BY_ID)
        r = a.savings_rate(JAN, "USD")
        assert r.value is None and "no income" in (r.unavailable_reason or "")

    def test_no_transactions_is_unavailable(self, analyzer: Analyzer) -> None:
        r = analyzer.savings_rate(DateRange.month("2025-06"), "USD")
        assert r.value is None and "no transactions" in (r.unavailable_reason or "")

    def test_unresolved_items_make_it_unavailable_instead_of_misleading(self, rows) -> None:  # noqa: ANN001
        strict = Analyzer(
            rows.values(), BY_ID, AnalyticsConfig(max_unresolved_ratio=Decimal("0.02"))
        )
        r = strict.savings_rate(JAN, "USD")  # 90.00 unresolved = 3% of income
        assert r.value is None and "could be misleading" in (r.unavailable_reason or "")

    def test_negative_savings_rate_is_reported_when_valid(self) -> None:
        a = Analyzer(
            [
                make("2024-01-02", "SALARY", "1000.00", "Salary"),
                make("2024-01-03", "RENT", "-1500.00", "Housing"),
            ],
            BY_ID,
        )
        assert a.savings_rate(JAN, "USD").value == Decimal("-0.5000")

    def test_other_currencies_are_named_in_a_warning(self, analyzer: Analyzer) -> None:
        assert any("EUR" in w for w in analyzer.savings_rate(JAN, "USD").warnings)
        assert analyzer.savings_rate(JAN, "EUR").value == Decimal("0.6000")  # (2000-800)/2000


class TestCurrencies:
    def test_totals_are_per_currency_and_never_mixed(self, analyzer: Analyzer) -> None:
        assert analyzer.currencies(JAN) == ["EUR", "USD"]
        eur = analyzer.summary(JAN, "EUR")
        assert eur.income == Money(200000, "EUR") and eur.expenses == Money(80000, "EUR")
        assert analyzer.summary(JAN, "USD").income == usd("3000.00")  # EUR salary not added

    def test_money_of_different_currencies_cannot_be_combined(self, analyzer: Analyzer) -> None:
        with pytest.raises(CurrencyMismatch):
            analyzer.summary(JAN, "EUR").income + analyzer.summary(JAN, "USD").income

    def test_unknown_currency_is_simply_empty(self, analyzer: Analyzer) -> None:
        assert analyzer.summary(JAN, "JPY").transaction_count == 0


class TestMonthlyComparisonsAndTrends:
    def test_monthly_view_includes_every_month_of_the_period(self, analyzer: Analyzer) -> None:
        flows = analyzer.monthly(DateRange(date(2024, 1, 1), date(2024, 3, 31)), "USD")
        assert [(f.month, f.has_data) for f in flows] == [
            ("2024-01", True),
            ("2024-02", True),
            ("2024-03", False),
        ]

    def test_month_over_month(self, analyzer: Analyzer) -> None:
        jan, feb = analyzer.month_over_month(JAN_FEB, "USD", "expenses")
        assert jan.previous is None and "No data for the previous month" in (jan.note or "")
        assert feb.current == usd("1327.10") and feb.previous == usd("1370.40")
        assert feb.delta == usd("-43.30") and feb.pct == Decimal("-0.0316")

    def test_flat_income_is_zero_change(self, analyzer: Analyzer) -> None:
        feb = analyzer.month_over_month(JAN_FEB, "USD", "income")[1]
        assert feb.delta == usd("0.00") and feb.pct == Decimal("0.0000")

    def test_previous_month_of_zero_has_no_percentage(self) -> None:
        a = Analyzer(
            [
                make("2024-01-02", "SALARY", "100.00", "Salary"),
                make("2024-02-03", "RENT", "-50.00", "Housing"),
            ],
            BY_ID,
        )
        feb = a.month_over_month(DateRange(date(2024, 1, 1), date(2024, 2, 29)), "USD", "expenses")[
            1
        ]
        assert feb.previous == usd("0.00") and feb.pct is None and "zero" in (feb.note or "")

    def test_a_month_without_data_is_not_a_hundred_percent_drop(self, analyzer: Analyzer) -> None:
        march = analyzer.month_over_month(DateRange(date(2024, 1, 1), date(2024, 3, 31)), "USD")[2]
        assert march.pct is None and "no transactions" in (march.note or "").lower()

    def test_invalid_metric_rejected(self, analyzer: Analyzer) -> None:
        with pytest.raises(ValueError):
            analyzer.month_over_month(JAN, "USD", "salary")

    def test_trend_needs_enough_months(self, analyzer: Analyzer, rows) -> None:  # noqa: ANN001
        short = analyzer.spending_trend(JAN_FEB, "USD")
        assert short.unavailable_reason and "at least 3 months" in short.unavailable_reason
        march = make("2024-03-04", "RENT MARCH", "-1200.00", "Housing")
        longer = Analyzer([*rows.values(), march], BY_ID).spending_trend(
            DateRange(date(2024, 1, 1), date(2024, 3, 31)), "USD"
        )
        assert longer.unavailable_reason is None
        assert [(m, v.to_decimal()) for m, v in longer.points] == [
            ("2024-01", Decimal("1370.40")),
            ("2024-02", Decimal("1327.10")),
            ("2024-03", Decimal("1200.00")),
        ]


class TestLargestAndMerchants:
    def test_largest_excludes_matched_transfers(self, analyzer: Analyzer, rows) -> None:  # noqa: ANN001
        top = analyzer.largest_transactions(JAN, "USD", n=3, direction="out")
        assert [t.id for t in top] == [
            rows["jan_rent"].id,
            rows["jan_atm"].id,
            rows["jan_vanguard"].id,
        ]

    def test_largest_in_and_any(self, analyzer: Analyzer, rows) -> None:  # noqa: ANN001
        assert (
            analyzer.largest_transactions(JAN, "USD", n=1, direction="in")[0].id
            == rows["jan_salary"].id
        )
        assert (
            analyzer.largest_transactions(JAN, "USD", n=1, direction="any")[0].id
            == rows["jan_salary"].id  # 3000.00 beats the 1200.00 rent by absolute amount
        )
        assert analyzer.largest_transactions(JAN, "USD", n=0) == []
        with pytest.raises(ValueError):
            analyzer.largest_transactions(JAN, "USD", direction="sideways")

    def test_merchant_summary(self, analyzer: Analyzer) -> None:
        summary = {m.merchant_key: m for m in analyzer.merchant_summary(JAN_FEB, "USD")}
        grocery = summary["GROCERY MART"]
        assert grocery.count == 2 and grocery.total == usd("177.50")  # 85.40 + 92.10
        assert grocery.average == Decimal("88.7500")
        assert "TRANSFER TO SAVINGS" not in summary  # matched transfers are not merchants


class TestEdgeCases:
    def test_voided_rows_are_ignored(self) -> None:
        a = Analyzer([make("2024-01-02", "SALARY", "100.00", "Salary", voided=True)], BY_ID)
        assert a.summary(JAN, "USD").transaction_count == 0

    def test_a_category_unknown_to_the_map_counts_as_uncategorized(self) -> None:
        t = make("2024-01-05", "MYSTERY", "-10.00", "Dining")
        a = Analyzer([t], {})  # category map lacks the id
        assert a.summary(JAN, "USD").uncategorized_out == usd("10.00")

    def test_changing_a_category_role_changes_the_treatment(self, rows) -> None:  # noqa: ANN001
        cats = dict(BY_ID)
        cash = CATEGORIES["Cash withdrawals"]
        cats[cash.id] = cash.model_copy(update={"role": CategoryRole.EXPENSE})
        assert Analyzer(rows.values(), cats).expenses(JAN, "USD").value == usd("1570.40")

    def test_zero_amount_rows_do_not_change_totals(self) -> None:
        a = Analyzer(
            [
                make("2024-01-02", "NOTHING", "0.00", "Dining"),
                make("2024-01-03", "SALARY", "10.00", "Salary"),
            ],
            BY_ID,
        )
        s = a.summary(JAN, "USD")
        assert s.expenses == usd("0.00") and s.income == usd("10.00") and s.raw_net == usd("10.00")

    def test_classification_buckets(self, rows) -> None:  # noqa: ANN001
        cls = {c.txn.id: c.bucket for c in classify(list(rows.values()), BY_ID, AnalyticsConfig())}
        assert cls[rows["jan_salary"].id] == Bucket.INCOME
        assert cls[rows["jan_to_savings"].id] == Bucket.TRANSFER_MATCHED
        assert cls[rows["jan_friend"].id] == Bucket.TRANSFER_UNMATCHED
        assert cls[rows["jan_atm"].id] == Bucket.CASH_WITHDRAWAL
        assert cls[rows["jan_vanguard"].id] == Bucket.SAVINGS
        assert cls[rows["jan_mystery_in"].id] == Bucket.UNCATEGORIZED_IN
        assert cls[rows["jan_mystery_out"].id] == Bucket.UNCATEGORIZED_OUT
        assert cls[rows["jan_shop_refund"].id] == Bucket.REFUND_LINKED
        assert cls[rows["feb_refund_unlinked"].id] == Bucket.REFUND_UNLINKED


CATEGORY_CHOICES = [None, *CATEGORIES]


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=-500_000, max_value=500_000).filter(lambda v: v != 0),
            st.sampled_from(CATEGORY_CHOICES),
            st.booleans(),  # part of a matched transfer?
            st.integers(min_value=1, max_value=28),
            st.sampled_from(["USD", "EUR"]),
        ),
        max_size=40,
    )
)
def test_property_buckets_always_account_for_every_transaction(items) -> None:  # noqa: ANN001
    txns = []
    for amount, category, grouped, day, currency in items:
        t = make(f"2024-01-{day:02d}", "ITEM", "1.00", category, currency=currency)
        txns.append(
            t.model_copy(
                update={"amount_minor": amount, "transfer_group_id": "g" if grouped else None}
            )
        )
    analyzer = Analyzer(txns, BY_ID)
    for currency in ("USD", "EUR"):
        s = analyzer.summary(JAN, currency)
        rebuilt = (
            s.net
            - s.cash_withdrawn
            + s.savings_net
            + s.matched_transfers_net
            + (s.unmatched_transfers_in - s.unmatched_transfers_out)
            + s.uncategorized_in
        )
        assert rebuilt == s.raw_net
        assert s.raw_credits + s.raw_debits == s.raw_net
        assert s.raw_net.minor == sum(t.amount_minor for t in txns if t.currency == currency)
        assert (
            sum(c.amount.minor for c in analyzer.category_spend(JAN, currency)) == s.expenses.minor
        )
