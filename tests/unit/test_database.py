from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bank_analyzer.database.connection import connect, connect_readonly, transaction
from bank_analyzer.database.migrations import (
    LATEST_VERSION,
    _statements,
    current_version,
    migrate,
    open_database,
)
from bank_analyzer.domain.errors import SchemaVersionError
from bank_analyzer.domain.models import DEFAULT_CATEGORIES

EXPECTED_TABLES = {
    "schema_version",
    "accounts",
    "categories",
    "import_profiles",
    "import_batches",
    "transactions",
    "categorization_rules",
    "merchant_mappings",
    "recurring_candidates",
    "anomaly_alerts",
    "settings",
    "audit_events",
}


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


class TestMigrations:
    def test_fresh_database_reaches_latest(self, conn: sqlite3.Connection) -> None:
        assert current_version(conn) == LATEST_VERSION
        assert tables(conn) == EXPECTED_TABLES

    def test_default_categories_seeded(self, conn: sqlite3.Connection) -> None:
        names = {r[0] for r in conn.execute("SELECT name FROM categories")}
        assert names == {n for n, _ in DEFAULT_CATEGORIES}
        assert len(names) == 21

    def test_migrate_is_idempotent(self, conn: sqlite3.Connection) -> None:
        assert migrate(conn) == LATEST_VERSION
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1

    def test_newer_database_refused(self, conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO schema_version VALUES (999, 'x')")
        with pytest.raises(SchemaVersionError):
            migrate(conn)

    def test_failed_migration_leaves_no_partial_schema(self) -> None:
        raw = connect(":memory:")
        try:
            with pytest.raises(sqlite3.OperationalError), transaction(raw):
                raw.execute("CREATE TABLE t1 (a INTEGER)")
                raw.execute("CREATE TABLE t1 (a INTEGER)")  # duplicate -> error
            assert tables(raw) == set()
        finally:
            raw.close()

    def test_file_database_created_and_reopened(self, tmp_path: Path) -> None:
        db = tmp_path / "sub" / "bank.sqlite3"
        first = open_database(db)
        first.execute("INSERT INTO settings VALUES ('k', 'v', 'now')")
        first.close()
        second = open_database(db)
        assert second.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "v"
        second.close()

    def test_upgrade_takes_backup_and_applies_new_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bank_analyzer.database import migrations

        db = tmp_path / "bank.sqlite3"
        first = open_database(db)
        first.execute("INSERT INTO settings VALUES ('k', 'v', 'now')")
        first.close()

        def m002(c: sqlite3.Connection) -> None:
            c.execute("ALTER TABLE settings ADD COLUMN note TEXT")

        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            (*migrations.MIGRATIONS, migrations.Migration(2, "add note", m002)),
        )
        monkeypatch.setattr(migrations, "LATEST_VERSION", 2)

        upgraded = open_database(db)
        assert current_version(upgraded) == 2
        assert upgraded.execute("SELECT note FROM settings").fetchone()[0] is None
        upgraded.close()

        backup = tmp_path / "bank.sqlite3.pre-v1.bak"
        assert backup.exists()
        old = sqlite3.connect(str(backup))
        try:
            assert old.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "v"
            assert "note" not in [r[1] for r in old.execute("PRAGMA table_info(settings)")]
        finally:
            old.close()

    def test_failed_upgrade_rolls_back_and_keeps_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bank_analyzer.database import migrations

        db = tmp_path / "bank.sqlite3"
        open_database(db).close()

        def bad(c: sqlite3.Connection) -> None:
            c.execute("ALTER TABLE settings ADD COLUMN half_done TEXT")
            c.execute("THIS IS NOT SQL")

        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            (*migrations.MIGRATIONS, migrations.Migration(2, "bad", bad)),
        )
        monkeypatch.setattr(migrations, "LATEST_VERSION", 2)
        with pytest.raises(sqlite3.OperationalError):
            open_database(db)

        monkeypatch.undo()
        again = open_database(db)
        assert current_version(again) == 1
        assert "half_done" not in [r[1] for r in again.execute("PRAGMA table_info(settings)")]
        again.close()

    def test_statement_splitter_handles_comments(self) -> None:
        stmts = _statements("-- c\nCREATE TABLE a (x INTEGER);\n-- d\nCREATE TABLE b (y TEXT);\n")
        assert len(stmts) == 2 and stmts[1].endswith(");")


class TestConnections:
    def test_foreign_keys_enforced(self, conn: sqlite3.Connection) -> None:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO import_batches (id, account_id, file_sha256, file_kind, "
                "row_count_read, row_count_imported, row_count_skipped, status, created_at) "
                "VALUES ('b', 'missing', 'h', 'csv', 0, 0, 0, 'committed', 'now')"
            )

    def test_transaction_commits_and_rolls_back(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            conn.execute("INSERT INTO settings VALUES ('a', '1', 'now')")
        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("INSERT INTO settings VALUES ('b', '2', 'now')")
            raise RuntimeError("boom")
        keys = {r[0] for r in conn.execute("SELECT key FROM settings")}
        assert keys == {"a"}

    def test_nested_transaction_joins_outer(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(RuntimeError), transaction(conn):
            with transaction(conn):
                conn.execute("INSERT INTO settings VALUES ('a', '1', 'now')")
            raise RuntimeError("outer failure")
        assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0

    def test_readonly_connection_cannot_write(self, tmp_path: Path) -> None:
        db = tmp_path / "bank.sqlite3"
        open_database(db).close()
        ro = connect_readonly(db)
        try:
            assert ro.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 21
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("INSERT INTO settings VALUES ('x', 'y', 'now')")
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("DELETE FROM categories")
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("CREATE TABLE evil (a INTEGER)")
        finally:
            ro.close()


class TestSchemaConstraints:
    def test_category_source_pairing_enforced(self, conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO accounts VALUES ('a','A','deposit','USD',NULL,NULL,1,'now')")
        conn.execute(
            "INSERT INTO import_batches VALUES ('b','a','h','csv',NULL,0,0,0,NULL,'committed','now',NULL)"
        )
        base = (
            "INSERT INTO transactions (id, account_id, import_batch_id, txn_date, "
            "original_description, normalized_description, amount_minor, currency, txn_type, "
            "category_id, category_source, source_ref_json, created_at, updated_at) "
            "VALUES ('t','a','b','2024-01-01','x','X',1,'USD','other',?,?,'{}','n','n')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, ("some-id", "none"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (None, "llm"))

    def test_invalid_enum_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO accounts VALUES ('a','A','bogus','USD',NULL,NULL,1,'now')")
