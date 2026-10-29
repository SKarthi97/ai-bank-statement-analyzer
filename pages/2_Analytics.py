"""Analytics: every figure shows its period, currency and definition."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st
from app_support import run_page

from bank_analyzer.database.repositories import AccountRepository, TransactionRepository
from bank_analyzer.domain.money import Money
from bank_analyzer.domain.periods import DateRange
from bank_analyzer.services.analytics import Analyzer
from bank_analyzer.services.anomaly_detection import AnomalyService, describe_alert
from bank_analyzer.services.reconciliation import ReconStatus, reconcile_account
from bank_analyzer.services.recurring import amount_changes, detect_recurring
from bank_analyzer.ui.context import AppContext
from bank_analyzer.ui.display import safe_cell
from bank_analyzer.ui.overview import available_months


def _period(ctx: AppContext) -> DateRange | None:
    months = available_months(ctx.conn)
    if not months:
        st.info("No transactions yet. Import a statement first.")
        return None
    mode = st.radio("Period", ["One month", "Several months", "Custom dates"], horizontal=True)
    if mode == "One month":
        return DateRange.month(st.selectbox("Month", months))
    if mode == "Several months":
        first, last = st.select_slider(
            "From / to", options=sorted(months), value=(min(months), max(months))
        )
        return DateRange(DateRange.month(first).start, DateRange.month(last).end)
    a, b = st.columns(2)
    start, end = (
        a.date_input("From", value=date.fromisoformat(f"{min(months)}-01")),
        b.date_input("To"),
    )
    if start > end:
        st.error("The start date is after the end date.")
        return None
    return DateRange(start, end)


def _figures(analyzer: Analyzer, period: DateRange, currency: str) -> None:
    s = analyzer.summary(period, currency)
    a, b, c, d = st.columns(4)
    a.metric("Income", s.income.format())
    b.metric("Expenses", s.expenses.format())
    c.metric("Net cash flow", s.net.format())
    d.metric("Cash withdrawn", s.cash_withdrawn.format())
    st.caption(
        "Expenses are net of refunds linked to their purchase. Transfers between your own accounts "
        "are excluded. Uncategorized money out counts as spending; uncategorized money in is not "
        "counted as income."
    )
    if not s.uncategorized_in.is_zero or not s.uncategorized_out.is_zero:
        st.warning(
            f"Uncategorized: {s.uncategorized_in.format()} in, {s.uncategorized_out.format()} out. "
            "Categorize them for accurate totals."
        )
    if not s.unmatched_transfers_in.is_zero or not s.unmatched_transfers_out.is_zero:
        st.info(
            "Some transfers have no matching other leg and are shown separately, not as spending."
        )
    rate = analyzer.savings_rate(period, currency)
    if rate.value is None:
        st.caption(f"Savings rate not shown: {rate.unavailable_reason}")
    else:
        st.metric("Savings rate", f"{Decimal(str(rate.value)) * 100:.1f}%")
        st.caption(rate.definition)


def _breakdowns(analyzer: Analyzer, period: DateRange, currency: str) -> None:
    cats = analyzer.category_spend(period, currency)
    st.subheader("Spending by category")
    st.dataframe(
        [{"Category": c.name, "Amount": c.amount.format(), "Transactions": c.count} for c in cats],
        hide_index=True,
    )
    if cats:
        chart = pd.DataFrame(
            {"Amount": [float(c.amount.to_decimal()) for c in cats]}, index=[c.name for c in cats]
        )
        st.bar_chart(chart)  # display only; the exact figures are in the table above
    st.subheader("Month over month (expenses)")
    st.dataframe(
        [
            {
                "Month": ch.month,
                "Expenses": ch.current.format(),
                "Change": ch.delta.format() if ch.delta else "",
                "Change %": f"{ch.pct * 100:.1f}%" if ch.pct is not None else "",
                "Note": ch.note or "",
            }
            for ch in analyzer.month_over_month(period, currency)
        ],
        hide_index=True,
    )
    trend = analyzer.spending_trend(period, currency)
    st.caption(trend.unavailable_reason or "Trend: monthly expenses for months with data.")
    st.subheader("Top merchants")
    st.dataframe(
        [
            {
                "Merchant": safe_cell(m.merchant_key),
                "Total": m.total.format(),
                "Count": m.count,
                "Average": str(m.average),
            }
            for m in analyzer.merchant_summary(period, currency)[:10]
        ],
        hide_index=True,
    )
    st.subheader("Largest expenses")
    st.dataframe(
        [
            {
                "Date": t.txn_date.isoformat(),
                "Description": safe_cell(t.normalized_description),
                "Amount": t.amount.format(),
            }
            for t in analyzer.largest_transactions(period, currency, n=10)
        ],
        hide_index=True,
    )


def _recurring(ctx: AppContext) -> None:
    st.subheader("Recurring payments")
    st.caption(
        "Merchants that charge or pay you about the same amount at a regular interval (at least 3 "
        "times). These are detected from your history, not confirmed by you."
    )
    candidates = detect_recurring(TransactionRepository(ctx.conn).fetch_all())
    if not candidates:
        st.write("None found yet.")
        return
    increased = {c.candidate.merchant_key for c in amount_changes(candidates)}
    st.dataframe(
        [
            {
                "Merchant": safe_cell(c.merchant_key),
                "Every": c.cadence_label,
                "Typical amount": Money(c.typical_amount_minor, c.currency).format(),
                "Latest": Money(c.last_amount_minor, c.currency).format(),
                "Times": c.occurrences,
                "Amount stable": "yes" if c.amount_stable else "no",
                "Recently higher": "yes" if c.merchant_key in increased else "",
            }
            for c in candidates
        ],
        hide_index=True,
    )


def _alerts(ctx: AppContext) -> None:
    st.subheader("Unusual activity")
    st.caption(
        "These are prompts to look, not conclusions. Each says which rule fired and what it compared "
        "against. Confirming or dismissing only records your decision; nothing else changes."
    )
    service = AnomalyService(ctx.conn)
    if st.button("Check for unusual activity"):
        report = service.run()
        st.write(f"New alerts: {sum(report.created.values())}.")
        for rule, reason in report.skipped.items():
            st.caption(f"{rule} not checked: {reason}")
    for alert in service.open_alerts():
        st.text(describe_alert(alert))
        a, b, _ = st.columns([1, 1, 6])
        if a.button("It's fine", key=f"dismiss_{alert.id}"):
            service.dismiss(alert.id)
            st.rerun()
        if b.button("Needs attention", key=f"confirm_{alert.id}"):
            service.confirm(alert.id)
            st.rerun()


def _reconciliation(ctx: AppContext) -> None:
    st.subheader("Balance check")
    accounts = AccountRepository(ctx.conn).list()
    if not accounts:
        return
    labels = {a.id: a.alias for a in accounts}
    account_id = st.selectbox("Account", list(labels), format_func=lambda i: labels[i])
    for item in reconcile_account(ctx.conn, account_id):
        r = item.result
        icon = {
            ReconStatus.OK: "OK",
            ReconStatus.MISMATCH: "MISMATCH",
            ReconStatus.NOT_AVAILABLE: "not available",
        }[r.status]
        st.write(
            f"Import from {item.created_at[:10]}: {icon}; {r.steps_checked} balance steps checked."
        )
        for note in r.notes:
            st.caption(note)
        if r.first_mismatch:
            m = r.first_mismatch
            delta = Money(m.delta_minor, r.currency).format() if r.currency else str(m.delta_minor)
            st.caption(f"First mismatch: row {m.row_index}; the balance differs by {delta}.")


def main(ctx: AppContext) -> None:
    period = _period(ctx)
    if period is None:
        return
    analyzer = Analyzer.from_db(ctx.conn)
    currencies = analyzer.currencies(period)
    if not currencies:
        st.info("No transactions in this period.")
    else:
        currency = st.selectbox("Currency", currencies)
        st.caption(f"Period {period.label()}, currency {currency}.")
        _figures(analyzer, period, currency)
        _breakdowns(analyzer, period, currency)
    _recurring(ctx)
    _alerts(ctx)
    _reconciliation(ctx)


run_page("Analytics", main)
