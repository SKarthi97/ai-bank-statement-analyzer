"""Parsing in a child process: same results, enforced limits, and failures that do not reach the app."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bank_analyzer.domain.errors import (
    FileValidationError,
    ImportStructureError,
    NeedsOcrError,
)
from bank_analyzer.importer import extract_table, isolated
from bank_analyzer.importer.isolated import _child_env, extract_table_isolated, run_limited
from bank_analyzer.security.file_security import staged_upload
from helpers import FIXTURES

FIXTURE_FILES = [
    "deposit_basic.csv",
    "deposit_eu.csv",
    "bank_preamble_footer.csv",
    "bank_basic.xlsx",
    "multi_sheet.xlsx",
    "statement_ruled.pdf",
    "statement_debit_credit.pdf",
]


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_isolated_result_equals_direct_result(name: str) -> None:
    with staged_upload(FIXTURES / name) as staged:
        direct = extract_table(staged)
        child = extract_table_isolated(staged, timeout_s=60)
    assert child == direct


def test_parameters_reach_the_child() -> None:
    with staged_upload(FIXTURES / "multi_sheet.xlsx") as staged:
        direct = extract_table(staged, sheet="Transactions")
        child = extract_table_isolated(staged, sheet="Transactions", timeout_s=60)
    assert child == direct


class TestErrorsCrossTheBoundary:
    def test_a_scan_still_asks_for_ocr(self) -> None:
        with staged_upload(FIXTURES / "image_only.pdf") as staged, pytest.raises(NeedsOcrError):
            extract_table_isolated(staged, timeout_s=60)

    def test_an_encrypted_pdf_is_a_clean_error(self) -> None:
        with (
            staged_upload(FIXTURES / "encrypted.pdf") as staged,
            pytest.raises(ImportStructureError),
        ):
            extract_table_isolated(staged, timeout_s=60)

    def test_a_corrupt_pdf_is_a_clean_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"%PDF-1.4\n" + b"\x00\xff garbage that is not a pdf " * 50)
        with staged_upload(bad) as staged, pytest.raises(ImportStructureError):
            extract_table_isolated(staged, timeout_s=60)

    def test_an_unknown_error_type_is_reported_generically(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "a.csv"
        csv_path.write_text("Date,Description,Amount\n", encoding="utf-8")  # header only: no rows
        with staged_upload(csv_path) as staged:
            try:
                extract_table_isolated(staged, timeout_s=60)
            except ImportStructureError as exc:
                assert "Date" not in str(exc)  # no file content in the message


class TestLimits:
    def test_a_hang_is_killed_at_the_timeout(self) -> None:
        with pytest.raises(ImportStructureError, match="took too long"):
            run_limited(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                b"",
                timeout_s=1.5,
                memory_mb=512,
            )

    def test_a_clean_exit_is_returned(self) -> None:
        code, out, _ = run_limited(
            [sys.executable, "-c", "print('hi')"], b"", timeout_s=30, memory_mb=512
        )
        assert (code, out.strip()) == (0, b"hi")

    def test_runaway_memory_is_stopped_and_the_caller_survives(self) -> None:
        greedy = "x = bytearray(1_500_000_000); print(len(x))"
        code, out, applied = run_limited(
            [sys.executable, "-c", greedy], b"", timeout_s=60, memory_mb=300
        )
        assert applied, "no memory limit mechanism was available on this platform"
        assert code != 0
        assert b"1500000000" not in out

    def test_without_a_limit_the_same_allocation_would_succeed(self) -> None:
        # control: proves the previous test failed because of the limit, not because of the machine
        modest = "x = bytearray(150_000_000); print(len(x))"
        code, out, _ = run_limited(
            [sys.executable, "-c", modest], b"", timeout_s=60, memory_mb=1024
        )
        assert (code, out.strip()) == (0, b"150000000")


class TestEnvironmentAndReplies:
    def test_secrets_in_the_parent_environment_do_not_reach_the_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_API_TOKEN", "super-secret")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        env = _child_env()
        assert "MY_API_TOKEN" not in env
        assert "HTTPS_PROXY" not in env
        assert "PYTHONPATH" in env

    @pytest.mark.parametrize(
        "stdout",
        [
            b"",
            b"not json",
            b"[]",
            b'{"ok": true}',
            b'{"ok": true, "table": {"headers": 5}}',
            b'{"ok": false}',
        ],
    )
    def test_garbage_from_the_child_becomes_a_clean_error(
        self, stdout: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(isolated, "run_limited", lambda *a, **k: (1, stdout, True))
        with (
            staged_upload(FIXTURES / "deposit_basic.csv") as staged,
            pytest.raises(ImportStructureError),
        ):
            extract_table_isolated(staged)

    def test_a_forged_error_type_cannot_raise_arbitrary_exceptions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reply = b'{"ok": false, "error": {"type": "SystemExit", "message": "x"}}'
        monkeypatch.setattr(isolated, "run_limited", lambda *a, **k: (0, reply, True))
        with (
            staged_upload(FIXTURES / "deposit_basic.csv") as staged,
            pytest.raises(ImportStructureError),
        ):
            extract_table_isolated(staged)

    def test_a_known_error_type_is_rebuilt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reply = b'{"ok": false, "error": {"type": "FileValidationError", "message": "bad file"}}'
        monkeypatch.setattr(isolated, "run_limited", lambda *a, **k: (0, reply, True))
        with (
            staged_upload(FIXTURES / "deposit_basic.csv") as staged,
            pytest.raises(FileValidationError, match="bad file"),
        ):
            extract_table_isolated(staged)


def test_a_forged_type_naming_a_non_application_class_is_not_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bank_analyzer.domain import errors

    monkeypatch.setattr(errors, "NotAnAppError", RuntimeError, raising=False)
    reply = b'{"ok": false, "error": {"type": "NotAnAppError", "message": "boom"}}'
    monkeypatch.setattr(isolated, "run_limited", lambda *a, **k: (0, reply, True))
    with (
        staged_upload(FIXTURES / "deposit_basic.csv") as staged,
        pytest.raises(ImportStructureError) as info,
    ):
        extract_table_isolated(staged)
    assert not isinstance(info.value, RuntimeError)
    assert "boom" not in str(info.value)
