"""Versioned schema migrations.

Each migration runs inside one transaction together with its ``schema_version`` row, so a
failure leaves the database unchanged. Applied migrations are never edited. Before upgrading an
existing database to a newer version a file backup is taken. A database newer than this code is
refused rather than opened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bank_analyzer.database.connection import MEMORY, connect, transaction
from bank_analyzer.domain.errors import SchemaVersionError
from bank_analyzer.domain.models import DEFAULT_CATEGORIES, new_id, utc_now_iso
from bank_analyzer.security.file_security import ensure_private_dir, restrict_file

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (no executescript, which would commit)."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return statements


def _m001_initial(conn: sqlite3.Connection) -> None:
    for statement in _statements(_SCHEMA_FILE.read_text(encoding="utf-8")):
        conn.execute(statement)
    now = utc_now_iso()
    conn.executemany(
        "INSERT INTO categories (id, name, kind, is_active, is_default, created_at) "
        "VALUES (?, ?, ?, 1, 1, ?)",
        [(new_id(), name, kind.value, now) for name, kind in DEFAULT_CATEGORIES],
    )


_M002_STATEMENTS = (
    # Analytics role of a category (see CategoryRole). Existing rows are mapped from their
    # seeded names or, for user-created categories, from their kind.
    "ALTER TABLE categories ADD COLUMN role TEXT NOT NULL DEFAULT 'expense' "
    "CHECK (role IN ('income','expense','transfer','cash_withdrawal','refund','savings'))",
    "UPDATE categories SET role = 'income' WHERE is_default = 1 AND name IN ('Income', 'Salary')",
    "UPDATE categories SET role = 'transfer' WHERE is_default = 1 AND name = 'Transfers'",
    "UPDATE categories SET role = 'cash_withdrawal' WHERE is_default = 1 AND name = 'Cash withdrawals'",
    "UPDATE categories SET role = 'refund' WHERE is_default = 1 AND name = 'Refunds'",
    "UPDATE categories SET role = 'savings' WHERE is_default = 1 AND name = 'Savings and investments'",
    "UPDATE categories SET role = 'income' WHERE is_default = 0 AND kind = 'income'",
    "UPDATE categories SET role = 'transfer' WHERE is_default = 0 AND kind = 'transfer'",
    "UPDATE categories SET role = 'savings' WHERE is_default = 0 AND kind = 'neutral'",
    # One alert per (rule, subject): re-running detection never recreates a dismissed alert.
    "ALTER TABLE anomaly_alerts ADD COLUMN dedupe_key TEXT",
    "CREATE UNIQUE INDEX idx_alerts_dedupe ON anomaly_alerts(dedupe_key) WHERE dedupe_key IS NOT NULL",
    "CREATE INDEX idx_recurring_merchant ON recurring_candidates(merchant_key, currency)",
)


def _m002_roles_and_alert_keys(conn: sqlite3.Connection) -> None:
    for statement in _M002_STATEMENTS:
        conn.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema", _m001_initial),
    Migration(2, "category roles, alert dedupe keys", _m002_roles_and_alert_keys),
)
LATEST_VERSION = MIGRATIONS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied schema version, or 0 for an empty database."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def _backup_before_upgrade(conn: sqlite3.Connection, db_path: Path, version: int) -> None:
    target = db_path.with_name(f"{db_path.name}.pre-v{version}.bak")
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    restrict_file(target)


def migrate(conn: sqlite3.Connection, *, db_path: Path | None = None) -> int:
    """Bring the database to the latest version and return it.

    Raises SchemaVersionError if the database is newer than this build.
    """
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise SchemaVersionError(
            f"database schema version {version} is newer than supported version "
            f"{LATEST_VERSION}; upgrade the application or restore a backup"
        )
    pending = [m for m in MIGRATIONS if m.version > version]
    if not pending:
        return version
    if version > 0 and db_path is not None:
        _backup_before_upgrade(conn, db_path, version)
    for migration in pending:
        with transaction(conn):
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (migration.version, utc_now_iso()),
            )
    return LATEST_VERSION


def open_database(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a database in a private directory and migrate it."""
    path = Path(db_path) if str(db_path) != MEMORY else None
    if path is not None:
        ensure_private_dir(path.parent)
    conn = connect(db_path)
    try:
        migrate(conn, db_path=path)
    except BaseException:
        conn.close()
        raise
    if path is not None:
        restrict_file(path)
    return conn
