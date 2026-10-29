"""AI Assistant: approved questions only, answered from computed figures with the evidence shown.

The assistant cannot write (it uses a read-only database connection), cannot run SQL (questions
become one of a fixed list of plans) and its wording is checked against the computed numbers. If
the local model is off, answers are plain templates built from the same figures.
"""

from __future__ import annotations

import streamlit as st
from app_support import run_page

from bank_analyzer.ai.assistant import Assistant
from bank_analyzer.ai.query_plan import CATALOGUE_HELP
from bank_analyzer.ui.context import AppContext, today
from bank_analyzer.ui.display import safe_cell

MAX_HISTORY = 10


def _show(reply) -> None:  # noqa: ANN001
    # st.text, never markdown: the answer can contain statement-derived text.
    st.text(reply.answer)
    if reply.options:
        st.write("Options: " + ", ".join(safe_cell(o, 40) for o in reply.options))
    result = reply.result
    if result is None:
        return
    st.caption(
        f"Answered by {'the local AI wording (numbers verified)' if reply.answer_source == 'model' else 'a fixed template'}; "
        f"question understood by {reply.plan_source}."
    )
    with st.expander("How this was calculated", expanded=False):
        st.text(result.definition)
        if result.periods:
            st.text("Period: " + " and ".join(p.label() for p in result.periods))
        if result.currency:
            st.text(f"Currency: {result.currency}")
    if result.table_rows:
        st.dataframe(
            [
                {c: safe_cell(v) for c, v in zip(result.table_columns, row, strict=False)}
                for row in result.table_rows
            ],
            hide_index=True,
        )
    if result.evidence:
        st.write(f"Evidence ({len(result.evidence)} of {result.transaction_count} transactions):")
        st.dataframe(
            [
                {
                    "Date": e.txn_date.isoformat(),
                    "Description": safe_cell(e.description),
                    "Amount": e.amount_text,
                    "Transaction ID": e.transaction_id,
                }
                for e in result.evidence
            ],
            hide_index=True,
        )


def main(ctx: AppContext) -> None:
    status = ctx.ai_status()
    if not status.available:
        st.info("The local AI is off, so answers use fixed templates. Everything else works.")
    with st.expander("What can I ask?"):
        for q in CATALOGUE_HELP:
            st.text(q)
    history: list = st.session_state.setdefault("assistant_history", [])
    with st.form("ask", clear_on_submit=True):
        question = st.text_input("Ask about your transactions", max_chars=500)
        submitted = st.form_submit_button("Ask")
    if submitted and question.strip():
        with ctx.readonly() as ro:
            reply = Assistant(ro, today(), ctx.llm() if status.available else None).ask(question)
        history.insert(0, reply)
        del history[MAX_HISTORY:]
    for reply in history:
        st.divider()
        st.text("Q: " + safe_cell(reply.question, 300))
        _show(reply)
    if history and st.button("Clear conversation"):
        history.clear()
        st.rerun()


run_page("AI Assistant", main)
