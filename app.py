"""Overview page. Run with: streamlit run app.py"""

from __future__ import annotations

import streamlit as st
from app_support import run_page

from bank_analyzer.ui.context import AppContext, today
from bank_analyzer.ui.overview import build_overview


def main(ctx: AppContext) -> None:
    overview = build_overview(ctx.conn, today())
    if overview.transactions == 0:
        st.info(
            "No transactions yet. Open **Import Center** in the sidebar to import a CSV, Excel or "
            "PDF statement."
        )
    cols = st.columns(5)
    cols[0].metric("Accounts", overview.accounts)
    cols[1].metric("Transactions", overview.transactions)
    cols[2].metric("Imports", overview.imports)
    cols[3].metric("Need review", overview.needs_review)
    cols[4].metric("Open alerts", overview.open_alerts)

    st.subheader(f"Last month ({overview.last_month.label()})")
    if not overview.last_month_summaries:
        st.write("No transactions last month.")
    for summary in overview.last_month_summaries:
        st.caption(f"Currency: {summary.currency} (currencies are never added together)")
        a, b, c, d = st.columns(4)
        a.metric("Income", summary.income.format())
        b.metric("Expenses", summary.expenses.format())
        c.metric("Net cash flow", summary.net.format())
        d.metric("Cash withdrawn", summary.cash_withdrawn.format())
        if not summary.uncategorized_in.is_zero or not summary.uncategorized_out.is_zero:
            st.warning(
                "Some transactions are uncategorized. Money in that is uncategorized is not "
                "counted as income. Review them on the Transactions page."
            )
    if overview.needs_review:
        st.info(f"{overview.needs_review} transactions need a category: see Transactions.")


run_page("Overview", main)
