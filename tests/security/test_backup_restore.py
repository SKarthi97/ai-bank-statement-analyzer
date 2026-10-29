"""Backups are encrypted and authenticated; restore is verified and never silently destructive."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from assistant_data import load_history
from bank_analyzer.database.migrations import LATEST_VERSION, open_database
from bank_analyzer.database.repositories import AccountRepository
from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.domain.models import AccountType
from bank_analyzer.services.backup import (
    MAGIC,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)

PASS = "correct horse battery staple"
FAST = (2**10, 8, 1)  # cheap key derivation for tests; production uses the default


@pytest.fixture
def source(tmp_path: Path):  # noqa: ANN201
    conn = open_database(tmp_path / "live" / "bank.sqlite3")
    load_history(conn, AccountRepository(conn).create("Checking", AccountType.DEPOSIT, "USD"))
    yield conn, tmp_path / "live" / "bank.sqlite3"
    conn.close()


def dump(path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT txn_date, normalized_description, amount_minor, currency FROM transactions "
                "ORDER BY txn_date, normalized_description, amount_minor"
            )
        ]
    finally:
        conn.close()


class TestCreate:
    def test_file_is_encrypted_and_contains_no_data(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        conn, _ = source
        info = create_backup(conn, tmp_path / "b.bankbak", PASS, kdf=FAST)
        blob = (tmp_path / "b.bankbak").read_bytes()
        assert blob.startswith(MAGIC)
        assert info.schema_version == LATEST_VERSION
        for needle in (b"SQLite format", b"FRESH MART", b"ACME PAYROLL", b"Checking"):
            assert needle not in blob

    def test_two_backups_of_the_same_data_differ(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        conn, _ = source
        create_backup(conn, tmp_path / "a.bankbak", PASS, kdf=FAST)
        create_backup(conn, tmp_path / "b.bankbak", PASS, kdf=FAST)
        assert (tmp_path / "a.bankbak").read_bytes() != (tmp_path / "b.bankbak").read_bytes()

    @pytest.mark.parametrize("passphrase", ["", "short", "elevenchars"])
    def test_short_passphrases_are_refused(self, source, tmp_path: Path, passphrase: str) -> None:  # noqa: ANN001
        with pytest.raises(BackupError, match="at least 12"):
            create_backup(source[0], tmp_path / "x.bankbak", passphrase, kdf=FAST)
        assert not (tmp_path / "x.bankbak").exists()

    def test_no_partial_file_is_left_behind(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        create_backup(source[0], tmp_path / "b.bankbak", PASS, kdf=FAST)
        assert sorted(p.name for p in tmp_path.glob("*.bankbak*")) == ["b.bankbak"]

    def test_backup_ignores_another_connections_uncommitted_change(
        self, source, tmp_path: Path
    ) -> None:  # noqa: ANN001
        conn, db_path = source
        other = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE accounts SET alias = 'UNCOMMITTED'")
            create_backup(conn, tmp_path / "b.bankbak", PASS, kdf=FAST)
            other.execute("ROLLBACK")
        finally:
            other.close()
        restore_backup(tmp_path / "b.bankbak", PASS, tmp_path / "r" / "bank.sqlite3")
        check = sqlite3.connect(str(tmp_path / "r" / "bank.sqlite3"))
        try:
            assert [r[0] for r in check.execute("SELECT alias FROM accounts")] == ["Checking"]
        finally:
            check.close()

    def test_backup_refuses_while_its_own_transaction_is_open(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        conn, _ = source
        conn.execute("BEGIN")
        try:
            with pytest.raises(BackupError, match="still in progress"):
                create_backup(conn, tmp_path / "b.bankbak", PASS, kdf=FAST)
        finally:
            conn.execute("ROLLBACK")
        assert not (tmp_path / "b.bankbak").exists()


class TestVerifyAndRestore:
    def make(self, source, tmp_path: Path) -> Path:  # noqa: ANN001
        create_backup(source[0], tmp_path / "b.bankbak", PASS, kdf=FAST)
        return tmp_path / "b.bankbak"

    def test_round_trip_equality(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        target = tmp_path / "restored" / "bank.sqlite3"
        restore_backup(backup, PASS, target)
        assert dump(target) == dump(source[1])
        assert len(dump(target)) == 15
        reopened = open_database(target)  # migrates/validates like a normal start
        reopened.close()

    def test_wrong_passphrase_fails_and_writes_nothing(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        target = tmp_path / "restored" / "bank.sqlite3"
        with pytest.raises(BackupError, match="Wrong passphrase"):
            restore_backup(backup, "another long passphrase", target)
        assert not target.exists()

    def test_every_tampered_region_is_detected(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        original = backup.read_bytes()
        header_len = struct.calcsize(">8sIII16s12s")
        for position in (8, 12, header_len - 1, header_len, len(original) // 2, len(original) - 1):
            damaged = bytearray(original)
            damaged[position] ^= 0x01
            bad = tmp_path / f"bad{position}.bankbak"
            bad.write_bytes(bytes(damaged))
            with pytest.raises(BackupError):
                verify_backup(bad, PASS)

    def test_truncated_and_foreign_files_are_rejected(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        cut = tmp_path / "cut.bankbak"
        cut.write_bytes(backup.read_bytes()[:-20])
        junk = tmp_path / "junk.bankbak"
        junk.write_bytes(b"not a backup at all, just text" * 10)
        empty = tmp_path / "empty.bankbak"
        empty.write_bytes(b"")
        for path in (cut, junk, empty):
            with pytest.raises(BackupError):
                verify_backup(path, PASS)

    def test_hostile_key_derivation_cost_is_refused(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        blob = bytearray(backup.read_bytes())
        struct.pack_into(">I", blob, 8, 2**30)  # a header asking for enormous scrypt work
        bad = tmp_path / "bomb.bankbak"
        bad.write_bytes(bytes(blob))
        with pytest.raises(BackupError, match="not a backup file"):
            verify_backup(bad, PASS)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(BackupError, match="could not be read"):
            verify_backup(tmp_path / "nope.bankbak", PASS)

    def test_restore_refuses_to_replace_without_confirmation(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        target = tmp_path / "other" / "bank.sqlite3"
        other = open_database(target)
        other.close()
        with pytest.raises(BackupError, match="already exists"):
            restore_backup(backup, PASS, target)
        assert dump(target) == []  # untouched

    def test_confirmed_replace_keeps_a_copy_of_the_old_database(
        self, source, tmp_path: Path
    ) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        target = tmp_path / "other" / "bank.sqlite3"
        old = open_database(target)
        AccountRepository(old).create("Old account", AccountType.CASH, "EUR")
        old.close()
        restore_backup(backup, PASS, target, overwrite=True)
        assert len(dump(target)) == 15
        kept = target.with_name(target.name + ".pre-restore.bak")
        check = sqlite3.connect(str(kept))
        try:
            aliases = [r[0] for r in check.execute("SELECT alias FROM accounts")]
        finally:
            check.close()
        assert aliases == ["Old account"]
        assert not target.with_name(target.name + ".restoring").exists()

    def test_database_from_a_newer_version_is_refused(self, source, tmp_path: Path) -> None:  # noqa: ANN001
        conn, _ = source
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, 'x')",
            (LATEST_VERSION + 1,),
        )
        try:
            create_backup(conn, tmp_path / "future.bankbak", PASS, kdf=FAST)
        except BankAnalyzerError as exc:
            assert "newer version" in str(exc)
        else:  # pragma: no cover
            pytest.fail("a newer-schema database must not be backed up as restorable")
        finally:
            conn.execute("DELETE FROM schema_version WHERE version = ?", (LATEST_VERSION + 1,))

    def test_failed_restore_leaves_the_existing_database_alone(
        self, source, tmp_path: Path
    ) -> None:  # noqa: ANN001
        backup = self.make(source, tmp_path)
        target = tmp_path / "other" / "bank.sqlite3"
        old = open_database(target)
        old.close()
        with pytest.raises(BackupError):
            restore_backup(backup, "wrong passphrase here", target, overwrite=True)
        assert dump(target) == []


class TestCorruptDatabaseInsideAValidBackup:
    """The file decrypts fine, but the database in it is damaged: restore must still refuse."""

    def make_corrupt(self, tmp_path: Path) -> Path:
        from bank_analyzer.services.backup import _encrypt

        db = tmp_path / "damaged.sqlite3"
        conn = open_database(db)
        load_history(conn, AccountRepository(conn).create("Checking", AccountType.DEPOSIT, "USD"))
        conn.close()
        raw = bytearray(db.read_bytes())
        page = 4096
        for offset in range(page * 3, min(len(raw), page * 6)):
            raw[offset] ^= 0xFF  # destroy several table pages
        backup = tmp_path / "damaged.bankbak"
        backup.write_bytes(_encrypt(bytes(raw), PASS, FAST))
        return backup

    def test_verify_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(BackupError):
            verify_backup(self.make_corrupt(tmp_path), PASS)

    def test_restore_refuses_and_writes_nothing(self, tmp_path: Path) -> None:
        target = tmp_path / "r" / "bank.sqlite3"
        with pytest.raises(BackupError):
            restore_backup(self.make_corrupt(tmp_path), PASS, target)
        assert not target.exists()
        assert not target.with_name(target.name + ".restoring").exists()


class TestSubtleDamageOnlyTheIntegrityCheckCanSee:
    def test_an_inconsistent_index_is_refused(self, tmp_path: Path) -> None:
        """The index no longer matches its table. Every query still runs; only integrity_check
        reports it (as a result row, not an exception)."""
        from bank_analyzer.services.backup import _encrypt

        db = tmp_path / "idx.sqlite3"
        conn = open_database(db)
        load_history(conn, AccountRepository(conn).create("Checking", AccountType.DEPOSIT, "USD"))
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = 'CREATE INDEX idx_txn_date ON transactions(amount_minor)' "
            "WHERE name = 'idx_txn_date'"
        )
        conn.execute("PRAGMA writable_schema = OFF")
        conn.close()
        probe = sqlite3.connect(str(db))
        try:
            assert (
                probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            )  # the damage is real
            probe.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()  # and queries still work
        finally:
            probe.close()
        backup = tmp_path / "idx.bankbak"
        backup.write_bytes(_encrypt(db.read_bytes(), PASS, FAST))
        with pytest.raises(BackupError, match="integrity"):
            verify_backup(backup, PASS)
