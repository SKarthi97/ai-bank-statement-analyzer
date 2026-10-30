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
from bank_analyzer.security.logging_config import shutdown_logging
from bank_analyzer.security.network_guard import guard_active, uninstall_guard
from bank_analyzer.security.passcode import set_passcode
from helpers import write_shaded_statement
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
        uninstall_guard()  # run_page installs the guard in this process
        shutdown_logging()  # and a log file handler


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


class TestSecurityWiring:
    def test_the_guard_and_scrubbed_log_are_set_up_on_every_page(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=False)
        seen: dict[str, bool] = {}
        fake = FakeStreamlit()
        original = fake.widget

        def spy(name: str):  # noqa: ANN202
            if name == "title":
                seen["guard"] = guard_active()
            return original(name)

        fake.widget = spy  # type: ignore[method-assign]
        run("app.py", monkeypatch, fake)
        assert (env / "logs").is_dir()
        assert not guard_active()  # the helper removed it again

    def test_the_guard_is_active_while_the_page_body_runs(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=False)
        fake = FakeStreamlit()
        states: list[bool] = []
        original = fake.widget

        def spy(name: str):  # noqa: ANN202
            if name == "metric":
                states.append(guard_active())
            return original(name)

        fake.widget = spy  # type: ignore[method-assign]
        run("app.py", monkeypatch, fake)
        assert states
        assert all(states)

    def test_the_guard_can_be_switched_off_by_setting(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANKAN_BLOCK_OUTBOUND", "0")
        seed(env, history=False)
        fake = FakeStreamlit()
        states: list[bool] = []
        original = fake.widget

        def spy(name: str):  # noqa: ANN202
            if name == "metric":
                states.append(guard_active())
            return original(name)

        fake.widget = spy  # type: ignore[method-assign]
        run("app.py", monkeypatch, fake)
        assert states
        assert not any(states)

    def test_a_passcode_blocks_the_page_until_unlocked(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=True)
        set_passcode(env, "open sesame 42", kdf=(2**10, 8, 1))
        locked = FakeStreamlit()
        run("app.py", monkeypatch, locked)
        assert "metric" not in locked.calls  # nothing of the app was shown
        assert "text_input" in locked.calls  # the passcode prompt was

        unlocked = FakeStreamlit()
        unlocked.session_state["unlocked"] = True
        run("app.py", monkeypatch, unlocked)
        assert "metric" in unlocked.calls

    def test_a_wrong_passcode_does_not_unlock(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=True)
        set_passcode(env, "open sesame 42", kdf=(2**10, 8, 1))
        fake = FakeStreamlit(clicks={"Unlock"})  # the stub's text input answers ""
        run("app.py", monkeypatch, fake)
        assert not fake.session_state.get("unlocked")
        assert "metric" not in fake.calls

    def test_the_transactions_page_offers_a_csv_download(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=True)
        fake = FakeStreamlit()
        run("pages/1_Transactions.py", monkeypatch, fake)
        assert "download_button" in fake.calls


class TestPhase8Pages:
    def test_analytics_lists_recurring_payments(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=True)
        fake = FakeStreamlit()
        run("pages/2_Analytics.py", monkeypatch, fake)
        rows = [r for table in fake.shown if isinstance(table, list) for r in table if "Every" in r]
        assert sorted(r["Merchant"] for r in rows) == [
            "ACME PAYROLL",
            "STREAMLY",
        ]  # payroll recurs too
        higher = {r["Merchant"] for r in rows if r["Recently higher"] == "yes"}
        assert higher == {"STREAMLY"}  # 9.99 twice, then 14.99; the salary did not change

    def test_settings_can_check_database_integrity(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=True)
        fake = FakeStreamlit(clicks={"Check database integrity"})
        run("pages/4_Settings.py", monkeypatch, fake)
        assert "Check database integrity" in fake.clicked
        assert "success" in fake.calls


class TestPasswordProtectedUpload:
    LABEL = "PDF password (only if the PDF asks for one)"

    def _locked_pdf(self, tmp_path: Path) -> bytes:
        path = tmp_path / "locked.pdf"
        write_shaded_statement(
            path, ("DATE", "PARTICULARS", "PAYMENTS", "RECEIPTS", "BALANCE"), "pw-12345"
        )
        return path.read_bytes()

    def _run(
        self, env: Path, monkeypatch: pytest.MonkeyPatch, data: bytes, typed: str
    ) -> FakeStreamlit:
        seed(env, history=False)
        fake = FakeStreamlit(
            clicks={"Read file"},
            upload=("locked.pdf", data),
            texts={self.LABEL: typed} if typed else None,
        )
        run("pages/0_Import_Center.py", monkeypatch, fake)
        return fake

    def test_a_locked_pdf_without_a_password_asks_for_one(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._run(env, monkeypatch, self._locked_pdf(tmp_path), "")
        assert (
            "warning",
            "The PDF is password-protected. Enter its password and read the file again.",
        ) in fake.messages
        assert "raw" not in fake.session_state

    def test_a_wrong_password_is_refused_without_echoing_it(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._run(env, monkeypatch, self._locked_pdf(tmp_path), "guess-wrong")
        assert ("error", "That password does not open the PDF.") in fake.messages
        assert all("guess-wrong" not in text for _, text in fake.messages)
        assert "raw" not in fake.session_state

    def test_the_right_password_reads_the_statement(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._run(env, monkeypatch, self._locked_pdf(tmp_path), "pw-12345")
        raw = fake.session_state["raw"]
        assert raw.headers == ("DATE", "PARTICULARS", "PAYMENTS", "RECEIPTS", "BALANCE")
        assert len(raw.rows) == 5
        assert "pw-12345" not in repr(fake.session_state)  # the password is never kept in state


def test_manual_mapping_starts_on_debit_credit_when_the_file_has_those_columns(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(env, history=False)
    fake = FakeStreamlit(
        clicks={"Read file"},
        upload=("s.pdf", (FIXTURES / "statement_debit_credit.pdf").read_bytes()),
    )
    run("pages/0_Import_Center.py", monkeypatch, fake)
    assert "raw" in fake.session_state
    assert not any("Choose either one amount column" in text for _, text in fake.messages)


class TestYearlessAndCurrencyGuidance:
    def _statement(self, tmp_path: Path) -> bytes:
        path = tmp_path / "s.pdf"
        write_shaded_statement(path, ("DATE", "PARTICULARS", "PAYMENTS", "RECEIPTS", "BALANCE"))
        return path.read_bytes()

    def test_manual_path_explains_dates_without_a_year(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=False)
        fake = FakeStreamlit(clicks={"Read file"}, upload=("s.pdf", self._statement(tmp_path)))
        run("pages/0_Import_Center.py", monkeypatch, fake)
        assert any("no year" in text and "Analyze the layout" in text for _, text in fake.messages)
        assert not any("No single date format fits" in text for _, text in fake.messages)

    def test_a_currency_mismatch_says_how_to_fix_it(
        self, env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(env, history=False)  # the seeded account is in USD
        fake = FakeStreamlit(
            clicks={"Read file", "Analyze the layout"},
            upload=("s.pdf", self._statement(tmp_path)),
        )
        run("pages/0_Import_Center.py", monkeypatch, fake)
        assert any("Open 'Create an account' above" in text for _, text in fake.messages)
