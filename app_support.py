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

from urllib.parse import urlparse  # noqa: E402

import streamlit as st  # noqa: E402

from bank_analyzer.domain.errors import BankAnalyzerError  # noqa: E402
from bank_analyzer.security.logging_config import configure_logging  # noqa: E402
from bank_analyzer.security.network_guard import install_guard  # noqa: E402
from bank_analyzer.security.passcode import (  # noqa: E402
    PasscodeLocked,
    check_passcode,
    has_passcode,
)
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
        _protect(ctx)
        if not _unlocked(ctx):
            return
        _sidebar(ctx)
        try:
            body(ctx)
        except BankAnalyzerError as exc:
            st.error(str(exc))
    finally:
        ctx.close()


def _protect(ctx: AppContext) -> None:
    """Scrubbed logging, and the outbound-connection guard unless it was switched off."""
    configure_logging(ctx.settings.data_dir)
    if ctx.settings.block_outbound:
        allowed = (
            [urlparse(ctx.settings.ollama_url).hostname or ""]
            if ctx.settings.allow_remote_llm
            else []
        )
        install_guard(allow_hosts=allowed)


def _unlocked(ctx: AppContext) -> bool:
    """The optional start-up passcode. Without one set, the app is open."""
    data_dir = ctx.settings.data_dir
    if not has_passcode(data_dir) or st.session_state.get("unlocked"):
        return True
    st.info("This app is protected by a passcode.")
    entered = st.text_input("Passcode", type="password")
    if st.button("Unlock"):
        try:
            if check_passcode(data_dir, entered):
                st.session_state["unlocked"] = True
                st.rerun()
            st.error("Wrong passcode.")
        except PasscodeLocked as exc:
            st.error(str(exc))
    return False


def _sidebar(ctx: AppContext) -> None:
    with st.sidebar:
        st.caption("All data stays on this computer. Nothing is sent to the internet.")
        status = ctx.ai_status()
        (st.success if status.available else st.warning)(status.message)
        if ctx.sync_warning:
            st.error(ctx.sync_warning)
