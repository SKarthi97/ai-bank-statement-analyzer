"""SQLite connections.

Connections run in autocommit mode (``isolation_level=None``) so that transactions are always
explicit, via ``transaction()``. Foreign keys are enforced on every connection. The AI assistant
must only ever receive a ``connect_readonly`` connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MEMORY = ":memory:"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-write connection with foreign keys enforced."""
    path = str(db_path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != MEMORY:
        conn.execute("PRAGMA journal_mode = WAL")
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if enabled != 1:  # pragma: no cover - indicates a broken SQLite build
        conn.close()
        raise RuntimeError("SQLite foreign key enforcement could not be enabled")
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection that cannot modify the database (file mode ro plus query_only)."""
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: commit on success, roll back on any exception.

    If a transaction is already open the block joins it and the outer owner commits or rolls
    back, so repository methods can call each other safely.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
