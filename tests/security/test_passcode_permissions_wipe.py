"""Passcode gate, permission audit and full wipe."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bank_analyzer.database.migrations import open_database
from bank_analyzer.security.passcode import (
    FILE_NAME,
    FREE_ATTEMPTS,
    PasscodeError,
    PasscodeLocked,
    check_passcode,
    clear_passcode,
    has_passcode,
    set_passcode,
)
from bank_analyzer.security.permissions import audit_private_path, parse_icacls
from bank_analyzer.services.wipe import CONFIRMATION, WipeError, database_files, wipe_data

FAST = (2**10, 8, 1)
GOOD = "open sesame 42"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class TestPasscode:
    def test_no_passcode_means_open(self, tmp_path: Path) -> None:
        assert not has_passcode(tmp_path)
        assert check_passcode(tmp_path, "anything")

    def test_set_and_check(self, tmp_path: Path) -> None:
        set_passcode(tmp_path, GOOD, kdf=FAST)
        assert has_passcode(tmp_path)
        assert check_passcode(tmp_path, GOOD)
        assert not check_passcode(tmp_path, "wrong passcode")
        assert not check_passcode(tmp_path, "")

    def test_the_passcode_is_not_stored(self, tmp_path: Path) -> None:
        set_passcode(tmp_path, GOOD, kdf=FAST)
        text = (tmp_path / FILE_NAME).read_text(encoding="utf-8")
        assert GOOD not in text
        assert {"salt", "hash"} <= set(json.loads(text))

    def test_same_passcode_gives_different_stored_values(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        set_passcode(a, GOOD, kdf=FAST)
        set_passcode(b, GOOD, kdf=FAST)
        assert (a / FILE_NAME).read_text() != (b / FILE_NAME).read_text()

    @pytest.mark.parametrize("bad", ["", "short", "1234567"])
    def test_short_passcodes_are_refused(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(PasscodeError, match="at least 8"):
            set_passcode(tmp_path, bad, kdf=FAST)
        assert not has_passcode(tmp_path)

    def test_changing_needs_the_current_passcode(self, tmp_path: Path) -> None:
        set_passcode(tmp_path, GOOD, kdf=FAST)
        with pytest.raises(PasscodeError, match="current passcode"):
            set_passcode(tmp_path, "a different one", kdf=FAST)
        set_passcode(tmp_path, "a different one", current=GOOD, kdf=FAST)
        assert check_passcode(tmp_path, "a different one")
        assert not check_passcode(tmp_path, GOOD)

    def test_clearing_needs_the_current_passcode(self, tmp_path: Path) -> None:
        set_passcode(tmp_path, GOOD, kdf=FAST)
        with pytest.raises(PasscodeError):
            clear_passcode(tmp_path, "nope nope nope")
        clear_passcode(tmp_path, GOOD)
        assert not has_passcode(tmp_path)

    def test_repeated_failures_lock_the_gate_even_for_the_right_passcode(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        set_passcode(tmp_path, GOOD, kdf=FAST, now=clock)
        for _ in range(FREE_ATTEMPTS):
            assert not check_passcode(tmp_path, "wrong guess", now=clock)
        with pytest.raises(PasscodeLocked) as locked:
            check_passcode(tmp_path, GOOD, now=clock)
        assert 1 <= locked.value.seconds <= 31
        clock.t += 31
        assert check_passcode(tmp_path, GOOD, now=clock)

    def test_the_lock_survives_a_restart_and_grows(self, tmp_path: Path) -> None:
        clock = Clock()
        set_passcode(tmp_path, GOOD, kdf=FAST, now=clock)
        for _ in range(FREE_ATTEMPTS):
            check_passcode(tmp_path, "wrong guess", now=clock)
        clock.t += 31
        assert not check_passcode(tmp_path, "wrong guess", now=clock)  # one more miss
        with pytest.raises(PasscodeLocked) as locked:  # a fresh process reads the same file
            check_passcode(tmp_path, GOOD, now=clock)
        assert locked.value.seconds > 31  # the lock doubled

    def test_success_resets_the_counter(self, tmp_path: Path) -> None:
        clock = Clock()
        set_passcode(tmp_path, GOOD, kdf=FAST, now=clock)
        for _ in range(FREE_ATTEMPTS - 1):
            check_passcode(tmp_path, "wrong guess", now=clock)
        assert check_passcode(tmp_path, GOOD, now=clock)
        for _ in range(FREE_ATTEMPTS - 1):
            assert not check_passcode(tmp_path, "wrong guess", now=clock)  # no lock yet
        assert check_passcode(tmp_path, GOOD, now=clock)

    def test_a_damaged_file_is_reported_not_opened(self, tmp_path: Path) -> None:
        (tmp_path / FILE_NAME).write_text('{"salt": "!!", "hash": "x"}', encoding="utf-8")
        with pytest.raises(PasscodeError, match="damaged"):
            check_passcode(tmp_path, GOOD)


ICACLS_OPEN = """\
C:\\data NT AUTHORITY\\SYSTEM:(OI)(CI)(F)
        BUILTIN\\Administrators:(OI)(CI)(F)
        BUILTIN\\Users:(OI)(CI)(RX)
        Everyone:(R)
        CONTOSO\\jane:(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""
ICACLS_PRIVATE = """\
C:\\data NT AUTHORITY\\SYSTEM:(OI)(CI)(F)
        BUILTIN\\Administrators:(OI)(CI)(F)
        CONTOSO\\jane:(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""


class TestPermissions:
    def test_parse_reports_broad_principals(self) -> None:
        assert parse_icacls(ICACLS_OPEN) == ["BUILTIN\\Users", "Everyone"]

    def test_parse_accepts_a_private_acl(self) -> None:
        assert parse_icacls(ICACLS_PRIVATE) == []

    def test_deny_entries_are_not_grants(self) -> None:
        assert parse_icacls("C:\\d Everyone:(DENY)(R)\n") == []

    def test_a_missing_path_has_nothing_to_report(self, tmp_path: Path) -> None:
        assert audit_private_path(tmp_path / "missing") == []

    @pytest.mark.skipif(os.name != "nt", reason="uses Windows icacls")
    def test_real_icacls_output_is_understood(self, tmp_path: Path) -> None:
        folder = tmp_path / "shared"
        folder.mkdir()
        before = audit_private_path(folder)
        assert not any("Everyone" in w for w in before)
        subprocess.run(
            ["icacls", str(folder), "/grant", "Everyone:(R)"], check=True, capture_output=True
        )
        after = audit_private_path(folder)
        assert after
        assert "Everyone" in after[0]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
    def test_group_readable_directory_is_flagged(self, tmp_path: Path) -> None:
        folder = tmp_path / "d"
        folder.mkdir()
        folder.chmod(0o750)
        assert audit_private_path(folder)
        folder.chmod(0o700)
        assert audit_private_path(folder) == []


class TestWipe:
    def setup_data(self, tmp_path: Path) -> tuple[Path, Path]:
        db = tmp_path / "bank.sqlite3"
        conn = open_database(db)
        conn.close()
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "bankanalyzer.log").write_text("line", encoding="utf-8")
        (tmp_path / "backups").mkdir()
        (tmp_path / "backups" / "a.bankbak").write_bytes(b"x" * 100)
        (tmp_path / FILE_NAME).write_text("{}", encoding="utf-8")
        (tmp_path / "bank.sqlite3.pre-v1.bak").write_bytes(b"old")
        return tmp_path, db

    def test_requires_the_typed_confirmation(self, tmp_path: Path) -> None:
        data, db = self.setup_data(tmp_path)
        for wrong in ("", "delete all data", "yes", CONFIRMATION + " "):
            with pytest.raises(WipeError):
                wipe_data(data, db, confirmation=wrong)
        assert db.exists()

    def test_removes_database_logs_passcode_and_copies_but_keeps_backups_by_default(
        self, tmp_path: Path
    ) -> None:
        data, db = self.setup_data(tmp_path)
        report = wipe_data(data, db, confirmation=CONFIRMATION)
        assert not db.exists()
        assert not (data / "logs").exists()
        assert not (data / FILE_NAME).exists()
        assert not (data / "bank.sqlite3.pre-v1.bak").exists()
        assert (data / "backups" / "a.bankbak").exists()
        assert report.failed == []
        assert "bank.sqlite3" in report.removed

    def test_backups_are_removed_only_when_asked(self, tmp_path: Path) -> None:
        data, db = self.setup_data(tmp_path)
        wipe_data(data, db, confirmation=CONFIRMATION, include_backups_folder=True)
        assert not (data / "backups").exists()

    def test_files_are_overwritten_before_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data, db = self.setup_data(tmp_path)
        seen: dict[str, bytes] = {}
        original_unlink = Path.unlink

        def spy(self: Path, *args: object, **kwargs: object) -> None:
            if self.name == "bankanalyzer.log":
                seen["content"] = self.read_bytes()
            original_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", spy)
        wipe_data(data, db, confirmation=CONFIRMATION)
        assert seen["content"] == b"\x00" * 4  # "line" was replaced by zeros first

    def test_the_report_states_the_limits(self, tmp_path: Path) -> None:
        data, db = self.setup_data(tmp_path)
        report = wipe_data(data, db, confirmation=CONFIRMATION)
        joined = " ".join(report.limits)
        assert "SSD" in joined
        assert "backups" in joined

    def test_database_files_lists_journals_and_copies(self, tmp_path: Path) -> None:
        data, db = self.setup_data(tmp_path)
        (tmp_path / "bank.sqlite3-wal").write_bytes(b"w")
        names = {p.name for p in database_files(db)}
        assert {"bank.sqlite3", "bank.sqlite3-wal", "bank.sqlite3.pre-v1.bak"} <= names
