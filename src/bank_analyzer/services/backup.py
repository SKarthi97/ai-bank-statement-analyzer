"""Encrypted database backup and restore.

File format (``.bankbak``)::

    magic "BANKBAK1" | scrypt N (u32) | r (u32) | p (u32) | salt (16) | nonce (12) | ciphertext+tag

The whole header is authenticated as AES-256-GCM associated data, so changing any header byte, the
ciphertext or the tag makes decryption fail. The key comes from the passphrase with scrypt; the
passphrase is never stored. A wrong passphrase and a damaged file are indistinguishable by design
(both fail authentication) and produce the same message.

The backup is taken with SQLite's online backup API (a consistent copy even while the app is
open), checked with ``PRAGMA integrity_check``, then encrypted. Restoring decrypts to a private
temporary file, checks integrity and schema version, keeps a copy of any database it replaces, and
swaps the file in atomically. The app must be closed while restoring.

Limits: the file is encrypted in memory (fine for personal databases; capped at 1 GB), and a weak
passphrase can be guessed offline. A 12-character minimum is enforced but not a guarantee.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from bank_analyzer.database.migrations import LATEST_VERSION
from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.security.file_security import ensure_private_dir, restrict_file

MAGIC = b"BANKBAK1"
MIN_PASSPHRASE = 12
MAX_BACKUP_BYTES = 1_000_000_000
DEFAULT_KDF = (2**15, 8, 1)
_MAX_N = 2**20  # refuse hostile files that ask for enormous key-derivation work
_HEADER = struct.Struct(">8sIII16s12s")
SUFFIX = ".bankbak"


class BackupError(BankAnalyzerError):
    """A backup could not be created, read or restored. Messages never contain data."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    size_bytes: int
    schema_version: int


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"The passphrase must be at least {MIN_PASSPHRASE} characters.")


def _encrypt(plain: bytes, passphrase: str, kdf: tuple[int, int, int]) -> bytes:
    n, r, p = kdf
    salt, nonce = os.urandom(16), os.urandom(12)
    header = _HEADER.pack(MAGIC, n, r, p, salt, nonce)
    key = _derive(passphrase, salt, n, r, p)
    return header + AESGCM(key).encrypt(nonce, plain, header)


def _decrypt(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < _HEADER.size + 16 or blob[: len(MAGIC)] != MAGIC:
        raise BackupError("This is not a backup file made by this application.")
    _, n, r, p, salt, nonce = _HEADER.unpack_from(blob)
    if not (2**10 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
        raise BackupError("This is not a backup file made by this application.")
    header = blob[: _HEADER.size]
    try:
        key = _derive(passphrase, salt, n, r, p)
        return AESGCM(key).decrypt(nonce, blob[_HEADER.size :], header)
    except (InvalidTag, ValueError) as exc:
        raise BackupError("Wrong passphrase, or the backup file is damaged.") from exc


def _check_database(path: Path) -> int:
    """Integrity-check a SQLite file and return its schema version."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise BackupError("The backup's database failed its integrity check.")
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError("The backup does not contain a valid database.") from exc
    version = int(row[0]) if row and row[0] is not None else 0
    if version > LATEST_VERSION:
        raise BackupError("The backup is from a newer version of the application.")
    return version


def create_backup(
    conn: sqlite3.Connection,
    dest: Path,
    passphrase: str,
    *,
    kdf: tuple[int, int, int] = DEFAULT_KDF,
) -> BackupInfo:
    """Write an encrypted, integrity-checked backup of ``conn``'s database to ``dest``."""
    _check_passphrase(passphrase)
    if conn.in_transaction:  # SQLite's backup would wait forever on its own open transaction
        raise BackupError("An operation is still in progress. Finish it and try again.")
    ensure_private_dir(dest.parent)
    with tempfile.TemporaryDirectory(prefix="bankbak-") as tmp:
        plain_path = Path(tmp) / "snapshot.sqlite3"
        snapshot = sqlite3.connect(str(plain_path))
        try:
            conn.backup(snapshot)
        finally:
            snapshot.close()
        version = _check_database(plain_path)
        plain = plain_path.read_bytes()
    blob = _encrypt(plain, passphrase, kdf)
    temp = dest.with_name(dest.name + ".part")
    temp.write_bytes(blob)
    restrict_file(temp)
    os.replace(temp, dest)
    return BackupInfo(dest, len(blob), version)


def _read(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("The backup file is too large.")
        return path.read_bytes()
    except OSError as exc:
        raise BackupError("The backup file could not be read.") from exc


def verify_backup(path: Path, passphrase: str) -> BackupInfo:
    """Decrypt ``path`` to a private temp file and check it; nothing is written elsewhere."""
    blob = _read(path)
    plain = _decrypt(blob, passphrase)
    with tempfile.TemporaryDirectory(prefix="bankbak-") as tmp:
        probe = Path(tmp) / "probe.sqlite3"
        probe.write_bytes(plain)
        version = _check_database(probe)
    return BackupInfo(path, len(blob), version)


def restore_backup(
    path: Path, passphrase: str, target_db: Path, *, overwrite: bool = False
) -> BackupInfo:
    """Restore a verified backup to ``target_db``. The application must not have it open."""
    blob = _read(path)
    plain = _decrypt(blob, passphrase)
    if target_db.exists() and not overwrite:
        raise BackupError("A database already exists there. Confirm that it may be replaced.")
    ensure_private_dir(target_db.parent)
    staged = target_db.with_name(target_db.name + ".restoring")
    try:
        staged.write_bytes(plain)
        version = _check_database(staged)
        restrict_file(staged)
        if target_db.exists():
            keep = target_db.with_name(target_db.name + ".pre-restore.bak")
            original = sqlite3.connect(str(target_db))
            try:
                copy = sqlite3.connect(str(keep))
                try:
                    original.backup(copy)
                finally:
                    copy.close()
            finally:
                original.close()
            restrict_file(keep)
        try:
            for sidecar in ("-wal", "-shm"):  # stale journal files belong to the replaced database
                with contextlib.suppress(FileNotFoundError):
                    Path(str(target_db) + sidecar).unlink()
            os.replace(staged, target_db)
        except PermissionError as exc:
            raise BackupError(
                "The database is in use. Close the application and try again."
            ) from exc
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()
    return BackupInfo(target_db, target_db.stat().st_size, version)


def backup_notice() -> str:
    """Text for the UI: why backups are encrypted and what is not protected."""
    return (
        "Backups are encrypted with your passphrase. If you forget it they cannot be opened, and a "
        "short or common passphrase can be guessed. Keep backups away from cloud-synced folders "
        "unless you accept that encrypted copies leave this computer."
    )
