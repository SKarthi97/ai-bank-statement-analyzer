"""Run every page script against a stand-in Streamlit (see tests/stub_streamlit.py).

Skipped when real Streamlit is installed: ``test_pages_apptest.py`` runs the pages for real then.
"""

from __future__ import annotations

import importlib.util
import runpy
import sqlite3
import sys
from pathlib import Path

import pytest

from assistant_data import TODAY, load_history
from bank_analyzer.database.migrations import open_database
from bank_analyzer.database.repositories import AccountRepository
from bank_analyzer.domain.models import AccountType
from stub_streamlit import FakeStreamlit, fake_pandas

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
PAGES = [
    "app.py",
    "pages/0_Import_Center.py",
    "pages/1_Transactions.py",
    "pages/2_Analytics.py",
    "pages/3_AI_Assistant.py",
    "pages/4_Settings.py",
]

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("streamlit") is not None,
    reason="real Streamlit is installed; test_pages_apptest.py covers the pages",
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("BANKAN_DATA_DIR", str(data))
    monkeypatch.setenv("BANKAN_OLLAMA_URL", "http://127.0.0.1:9")  # nothing listens: AI offline
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    return data


def run(page: str, monkeypatch: pytest.MonkeyPatch, fake: FakeStreamlit) -> None:
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setattr(
        "bank_analyzer.ui.context.today", lambda: TODAY
    )  # the history is dated 2024
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas())
    monkeypatch.delitem(sys.modules, "app_support", raising=False)
    try:
        runpy.run_path(str(ROOT / page), run_name="__main__")
    finally:
        sys.modules.pop("app_support", None)


def seed(data: Path, *, history: bool) -> None:
    data.mkdir(parents=True, exist_ok=True)
    conn = open_database(data / "bank.sqlite3")
    account = AccountRepository(conn).create("Checking", AccountType.DEPOSIT, "USD")
    if history:
        load_history(conn, account)
    conn.close()


@pytest.mark.parametrize("history", [False, True], ids=["empty database", "with history"])
@pytest.mark.parametrize("page", PAGES)
def test_page_runs(page: str, history: bool, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(env, history=history)
    fake = FakeStreamlit()
    run(page, monkeypatch, fake)
    assert "title" in fake.calls  # the page rendered its chrome


def test_overview_shows_the_history(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(env, history=True)
    fake = FakeStreamlit()
    run("app.py", monkeypatch, fake)
    assert fake.calls.count("metric") >= 9  # five counters plus last month's four figures


def test_the_import_center_imports_a_real_file_end_to_end(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(env, history=False)
    data = (FIXTURES / "deposit_basic.csv").read_bytes()
    fake = FakeStreamlit(
        clicks={
            "Read file",
            "Preview import (nothing is saved)",
            "Save this import",
            "Import",
            "Categorize this import",
        },
        upload=("deposit_basic.csv", data),
    )
    run("pages/0_Import_Center.py", monkeypatch, fake)
    assert fake.clicked.count("Import") == 1
    conn = sqlite3.connect(env / "bank.sqlite3")
    try:
        stored = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        categorized = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_id IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == 9
    assert categorized == 9  # built-in rules cover this fixture


def test_the_import_is_not_saved_without_the_confirmation_box(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(env, history=False)
    data = (FIXTURES / "deposit_basic.csv").read_bytes()
    fake = FakeStreamlit(
        clicks={
            "Read file",
            "Preview import (nothing is saved)",
            "Import",
        },  # no "Save this import"
        upload=("deposit_basic.csv", data),
    )
    run("pages/0_Import_Center.py", monkeypatch, fake)
    conn = sqlite3.connect(env / "bank.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        conn.close()
