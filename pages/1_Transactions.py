"""Transactions: search and filter, and the human review queue for categories."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from app_support import run_page

from bank_analyzer.database.repositories import (
    AccountRepository,
    CategoryRepository,
    PageRequest,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.models import ReviewStatus
from bank_analyzer.services.categorization import Categorizer
from bank_analyzer.ui.context import AppContext
from bank_analyzer.ui.display import page_summary, transaction_rows

ANY = "(any)"


def _browse(ctx: AppContext) -> None:
    accounts = {a.id: a.alias for a in AccountRepository(ctx.conn).list(active_only=False)}
    categories = CategoryRepository(ctx.conn).list(active_only=False)
    by_id = {c.id: c for c in categories}
    c1, c2, c3, c4 = st.columns(4)
    account = c1.selectbox(
        "Account", [ANY, *accounts], format_func=lambda i: i if i == ANY else accounts[i]
    )
    category = c2.selectbox(
        "Category", [ANY, *by_id], format_func=lambda i: i if i == ANY else by_id[i].name
    )
    status = c3.selectbox("Review status", [ANY, *[s.value for s in ReviewStatus]])
    text = c4.text_input("Search description")
    d1, d2, d3 = st.columns(3)
    use_dates = d1.checkbox("Filter by date")
    start = d2.date_input("From", disabled=not use_dates)
    end = d3.date_input("To", disabled=not use_dates)
    uncategorized = st.checkbox("Uncategorized only")
    if use_dates and start > end:
        st.error("The start date is after the end date.")
        return
    flt = TransactionFilter(
        account_id=None if account == ANY else account,
        category_id=None if category == ANY else category,
        review_status=None if status == ANY else ReviewStatus(status),
        text=text.strip() or None,
        date_from=start if use_dates else None,
        date_to=end if use_dates else None,
        uncategorized=uncategorized,
    )
    p1, p2 = st.columns(2)
    size = p1.selectbox("Rows per page", [25, 50, 100, 200], index=1)
    number = p2.number_input("Page", min_value=1, value=1, step=1)
    page = TransactionRepository(ctx.conn).query(flt, PageRequest(page=int(number), page_size=size))
    st.caption(page_summary(page))
    st.dataframe(transaction_rows(page.items, by_id), hide_index=True)


def _review(ctx: AppContext) -> None:
    categorizer = Categorizer(ctx.conn, None, batch_size=ctx.settings.llm_batch_size)
    categories = [c for c in CategoryRepository(ctx.conn).list() if c.is_active]
    by_id = {c.id: c for c in categories}
    queue = categorizer.review_queue(page=1, page_size=100)
    st.caption(
        f"{queue.total} transactions wait for review. Suggestions from rules or the local AI are "
        "never final until you approve or correct them."
    )
    if not queue.items:
        st.success("Nothing to review.")
        return
    frame = pd.DataFrame(transaction_rows(queue.items, by_id))
    frame.insert(0, "Select", False)
    edited = st.data_editor(
        frame,
        hide_index=True,
        disabled=[c for c in frame.columns if c != "Select"],
        key="review_table",
    )
    chosen = edited.loc[edited["Select"], "ID"].tolist()
    st.write(f"{len(chosen)} selected")
    remember = st.checkbox(
        "Remember the merchant so future rows are categorized the same way", value=True
    )
    a, b, c = st.columns(3)
    if a.button("Approve suggested category", disabled=not chosen):
        result = categorizer.approve(chosen, remember_merchant=remember)
        st.success(
            f"Approved {result.changed}; skipped {result.skipped} (no suggestion to approve)."
        )
        st.rerun()
    target = b.selectbox("Set category to", list(by_id), format_func=lambda i: by_id[i].name)
    if b.button("Apply category", disabled=not chosen):
        result = categorizer.correct(chosen, target, remember_merchant=remember)
        st.success(f"Categorized {result.changed} transactions.")
        st.rerun()
    if c.button("Reject suggestion", disabled=not chosen):
        st.success(f"Removed {categorizer.reject_suggestions(chosen)} suggestions.")
        st.rerun()


def main(ctx: AppContext) -> None:
    browse, review = st.tabs(["All transactions", "Review queue"])
    with browse:
        _browse(ctx)
    with review:
        _review(ctx)


run_page("Transactions", main)
