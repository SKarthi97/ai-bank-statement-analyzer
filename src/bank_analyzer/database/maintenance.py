"""Database health checks.

Reports counts and generic messages only: never row contents, so the result is safe to show and log.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MAX_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    ok: bool
    problems: tuple[str, ...]  # SQLite's own structural messages (no data values)
    foreign_key_violations: int  # rows that reference a missing parent
    schema_version: int

    @property
    def summary(self) -> str:
        if self.ok:
            return "The database passed its integrity and foreign-key checks."
        parts = []
        if self.problems:
            parts.append(f"{len(self.problems)} structural problem(s)")
        if self.foreign_key_violations:
            parts.append(f"{self.foreign_key_violations} row(s) refer to a missing record")
        return "Problems found: " + ", ".join(parts) + ". Restore a backup if the data looks wrong."


def check_integrity(conn: sqlite3.Connection) -> IntegrityReport:
    """Run ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check``. Read-only."""
    try:
        messages = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError:
        messages = ["The database file is damaged and could not be read."]
    problems = tuple(m for m in messages if m != "ok")[:MAX_MESSAGES]
    try:
        violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.DatabaseError:
        violations = 0
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        version = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.DatabaseError:
        version = 0
    return IntegrityReport(not problems and violations == 0, problems, violations, version)
