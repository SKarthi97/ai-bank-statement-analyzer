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
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == LATEST_VERSION

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
        before = LATEST_VERSION
        after = before + 1

        def add_note(c: sqlite3.Connection) -> None:
            c.execute("ALTER TABLE settings ADD COLUMN note TEXT")

        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            (*migrations.MIGRATIONS, migrations.Migration(after, "add note", add_note)),
        )
        monkeypatch.setattr(migrations, "LATEST_VERSION", after)

        upgraded = open_database(db)
        assert current_version(upgraded) == after
        assert upgraded.execute("SELECT note FROM settings").fetchone()[0] is None
        upgraded.close()

        backup = tmp_path / f"bank.sqlite3.pre-v{before}.bak"
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

        before = LATEST_VERSION
        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            (*migrations.MIGRATIONS, migrations.Migration(before + 1, "bad", bad)),
        )
        monkeypatch.setattr(migrations, "LATEST_VERSION", before + 1)
        with pytest.raises(sqlite3.OperationalError):
            open_database(db)

        monkeypatch.undo()
        again = open_database(db)
        assert current_version(again) == before
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


class TestUpgradeFromVersion1:
    """A database created before category roles existed must upgrade without losing anything."""

    @staticmethod
    def make_v1(path: Path) -> sqlite3.Connection:
        from bank_analyzer.database import migrations

        conn = connect(path)
        with transaction(conn):
            migrations.MIGRATIONS[0].apply(conn)
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, 'x')")
        return conn

    def test_roles_are_mapped_and_user_data_survives(self, tmp_path: Path) -> None:
        db = tmp_path / "old.sqlite3"
        conn = self.make_v1(db)
        assert "role" not in [r[1] for r in conn.execute("PRAGMA table_info(categories)")]
        for name, kind in (
            ("My income", "income"),
            ("My transfer", "transfer"),
            ("My misc", "neutral"),
            ("My spend", "expense"),
        ):
            conn.execute(
                "INSERT INTO categories (id, name, kind, is_active, is_default, created_at) "
                "VALUES (?, ?, ?, 1, 0, 'x')",
                (name, name, kind),
            )
        conn.execute("INSERT INTO settings VALUES ('keep', 'me', 'x')")
        conn.close()

        upgraded = open_database(db)
        roles = {
            r["name"]: r["role"] for r in upgraded.execute("SELECT name, role FROM categories")
        }
        assert roles["Salary"] == "income" and roles["Income"] == "income"
        assert roles["Transfers"] == "transfer"
        assert roles["Cash withdrawals"] == "cash_withdrawal"
        assert roles["Refunds"] == "refund"
        assert roles["Savings and investments"] == "savings"
        assert roles["Groceries"] == "expense" and roles["Other"] == "expense"
        assert roles["My income"] == "income" and roles["My transfer"] == "transfer"
        assert roles["My misc"] == "savings" and roles["My spend"] == "expense"
        assert upgraded.execute("SELECT value FROM settings WHERE key='keep'").fetchone()[0] == "me"
        assert current_version(upgraded) == LATEST_VERSION
        assert (tmp_path / "old.sqlite3.pre-v1.bak").exists()
        upgraded.close()

    def test_invalid_role_is_rejected_by_the_database(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE categories SET role = 'bogus' WHERE name = 'Dining'")

    def test_alert_dedupe_index_prevents_duplicates(self, conn: sqlite3.Connection) -> None:
        sql = (
            "INSERT INTO anomaly_alerts (id, rule_id, details_json, status, created_at, dedupe_key) "
            "VALUES (?, 'A1', '{}', 'open', 'x', ?)"
        )
        conn.execute(sql, ("a1", "k"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, ("a2", "k"))
        conn.execute(sql, ("a3", None))  # alerts without a key are not constrained
        conn.execute(sql, ("a4", None))
