"""Shared setup for the Streamlit pages.

Every page runs through ``run_page``: it puts ``src`` on the import path, sets the page chrome,
opens a database connection for this run only (SQLite connections are bound to a thread and
Streamlit reruns scripts on varying threads), shows the privacy notice and AI status in the
sidebar, turns expected application errors into a readable message, and always closes the
connection.

Untrusted text (statement descriptions, merchant names, model output) must be shown with
``st.text`` or ``st.dataframe`` and never through markdown or ``unsafe_allow_html``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from bank_analyzer.domain.errors import BankAnalyzerError  # noqa: E402
from bank_analyzer.ui.context import AppContext, open_context  # noqa: E402


def run_page(title: str, body: Callable[[AppContext], None]) -> None:
    st.set_page_config(page_title=f"{title} - Bank Statement Analyzer", layout="wide")
    st.title(title)
    try:
        ctx = open_context()
    except BankAnalyzerError as exc:
        st.error(str(exc))
        return
    try:
        _sidebar(ctx)
        try:
            body(ctx)
        except BankAnalyzerError as exc:
            st.error(str(exc))
    finally:
        ctx.close()


def _sidebar(ctx: AppContext) -> None:
    with st.sidebar:
        st.caption("All data stays on this computer. Nothing is sent to the internet.")
        status = ctx.ai_status()
        (st.success if status.available else st.warning)(status.message)
        if ctx.sync_warning:
            st.error(ctx.sync_warning)
