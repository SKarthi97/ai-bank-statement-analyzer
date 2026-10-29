"""Run the pages with Streamlit's own test runner (``streamlit.testing.v1.AppTest``).

Skipped until Streamlit is installed (it is not in the environment these tests were written in).
**These tests have never been run**; if one fails on first use, check the AppTest API version
before assuming the page is wrong. The stub-based tests in ``test_pages_stub.py`` have run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from assistant_data import load_history  # noqa: E402
from bank_analyzer.database.migrations import open_database  # noqa: E402
from bank_analyzer.database.repositories import AccountRepository  # noqa: E402
from bank_analyzer.domain.models import AccountType  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PAGES = [
    "app.py",
    "pages/0_Import_Center.py",
    "pages/1_Transactions.py",
    "pages/2_Analytics.py",
    "pages/3_AI_Assistant.py",
    "pages/4_Settings.py",
]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("BANKAN_DATA_DIR", str(data))
    monkeypatch.setenv("BANKAN_OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    conn = open_database(data / "bank.sqlite3")
    load_history(conn, AccountRepository(conn).create("Checking", AccountType.DEPOSIT, "USD"))
    conn.close()
    return data


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_an_exception(page: str) -> None:
    app = AppTest.from_file(str(ROOT / page), default_timeout=30).run()
    assert not app.exception, [e.value for e in app.exception]


def test_assistant_page_answers_a_question() -> None:
    app = AppTest.from_file(str(ROOT / "pages/3_AI_Assistant.py"), default_timeout=30).run()
    app.text_input[0].set_value("show spending by category last month")
    app.button[0].click().run()
    assert not app.exception
