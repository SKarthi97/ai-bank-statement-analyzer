"""Repositories: the only place SQL is written. All statements are parameterized.

Sort columns are chosen from fixed allowlists, never interpolated from caller input.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Generic, Literal, TypeVar

from bank_analyzer.database.connection import transaction
from bank_analyzer.domain.errors import InvalidStateError, NotFoundError
from bank_analyzer.domain.models import (
    Account,
    AccountType,
    BatchStatus,
    CanonicalTransaction,
    Category,
    CategoryKind,
    CategorySource,
    ImportBatch,
    ImportSummary,
    ReviewStatus,
    RollbackSummary,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.domain.money import Money

T = TypeVar("T")


class AuditRepository:
    """Minimal audit trail: event name, entity id and a count. Never statement content."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, event: str, entity_id: str | None = None, count: int | None = None) -> None:
        self._conn.execute(
            "INSERT INTO audit_events (event, entity_id, count, created_at) VALUES (?, ?, ?, ?)",
            (event, entity_id, count, utc_now_iso()),
        )

    def list(self, limit: int = 100) -> list[tuple[str, str | None, int | None]]:
        rows = self._conn.execute(
            "SELECT event, entity_id, count FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(r["event"], r["entity_id"], r["count"]) for r in rows]


class SettingsRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utc_now_iso()),
        )


class AccountRepository:
    def __init__(self, conn: sqlite3.Connection, audit: AuditRepository | None = None) -> None:
        self._conn = conn
        self._audit = audit or AuditRepository(conn)

    @staticmethod
    def _row(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            alias=row["alias"],
            account_type=AccountType(row["account_type"]),
            currency=row["currency"],
            last4=row["last4"],
            opening_balance_minor=row["opening_balance_minor"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    def create(
        self,
        alias: str,
        account_type: AccountType,
        currency: str,
        *,
        last4: str | None = None,
        opening_balance_minor: int | None = None,
    ) -> Account:
        """Create an account alias. Raises InvalidStateError if the alias is already used."""
        account = Account(
            id=new_id(),
            alias=alias.strip(),
            account_type=account_type,
            currency=currency,
            last4=last4,
            opening_balance_minor=opening_balance_minor,
            created_at=utc_now_iso(),
        )
        try:
            self._conn.execute(
                "INSERT INTO accounts (id, alias, account_type, currency, last4, "
                "opening_balance_minor, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    account.id,
                    account.alias,
                    account.account_type.value,
                    account.currency,
                    account.last4,
                    account.opening_balance_minor,
                    account.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise InvalidStateError("an account with this alias already exists") from exc
        return account

    def get(self, account_id: str) -> Account:
        row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise NotFoundError("account not found")
        return self._row(row)

    def list(self, *, active_only: bool = True) -> list[Account]:
        sql = "SELECT * FROM accounts" + (" WHERE is_active = 1" if active_only else "")
        return [self._row(r) for r in self._conn.execute(sql + " ORDER BY alias").fetchall()]


class CategoryRepository:
    def __init__(self, conn: sqlite3.Connection, audit: AuditRepository | None = None) -> None:
        self._conn = conn
        self._audit = audit or AuditRepository(conn)

    @staticmethod
    def _row(row: sqlite3.Row) -> Category:
        return Category(
            id=row["id"],
            name=row["name"],
            kind=CategoryKind(row["kind"]),
            is_active=bool(row["is_active"]),
            is_default=bool(row["is_default"]),
            merged_into_id=row["merged_into_id"],
            created_at=row["created_at"],
        )

    def list(self, *, active_only: bool = True) -> list[Category]:
        sql = "SELECT * FROM categories" + (" WHERE is_active = 1" if active_only else "")
        return [self._row(r) for r in self._conn.execute(sql + " ORDER BY name").fetchall()]

    def get(self, category_id: str) -> Category:
        row = self._conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            raise NotFoundError("category not found")
        return self._row(row)

    def get_by_name(self, name: str) -> Category:
        row = self._conn.execute("SELECT * FROM categories WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise NotFoundError("category not found")
        return self._row(row)

    def create(self, name: str, kind: CategoryKind) -> Category:
        category = Category(id=new_id(), name=name.strip(), kind=kind, created_at=utc_now_iso())
        try:
            self._conn.execute(
                "INSERT INTO categories (id, name, kind, is_active, is_default, created_at) "
                "VALUES (?, ?, ?, 1, 0, ?)",
                (category.id, category.name, category.kind.value, category.created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise InvalidStateError("a category with this name already exists") from exc
        return category

    def rename(self, category_id: str, new_name: str) -> Category:
        self.get(category_id)
        try:
            self._conn.execute(
                "UPDATE categories SET name = ? WHERE id = ?", (new_name.strip(), category_id)
            )
        except sqlite3.IntegrityError as exc:
            raise InvalidStateError("a category with this name already exists") from exc
        return self.get(category_id)

    def set_active(self, category_id: str, active: bool) -> None:
        self.get(category_id)
        self._conn.execute(
            "UPDATE categories SET is_active = ? WHERE id = ?", (1 if active else 0, category_id)
        )

    def merge(self, source_id: str, target_id: str) -> int:
        """Move everything from ``source`` into ``target`` and disable ``source``.

        This is an explicit user action, so approved transactions are moved too. Returns the
        number of transactions moved. Atomic.
        """
        if source_id == target_id:
            raise InvalidStateError("cannot merge a category into itself")
        self.get(source_id)
        target = self.get(target_id)
        if not target.is_active:
            raise InvalidStateError("the target category is disabled")
        now = utc_now_iso()
        with transaction(self._conn):
            moved = self._conn.execute(
                "UPDATE transactions SET category_id = ?, updated_at = ? WHERE category_id = ?",
                (target_id, now, source_id),
            ).rowcount
            self._conn.execute(
                "UPDATE categorization_rules SET category_id = ? WHERE category_id = ?",
                (target_id, source_id),
            )
            self._conn.execute(
                "UPDATE merchant_mappings SET category_id = ? WHERE category_id = ?",
                (target_id, source_id),
            )
            self._conn.execute(
                "UPDATE categories SET is_active = 0, merged_into_id = ? WHERE id = ?",
                (target_id, source_id),
            )
            self._audit.record("category_merged", source_id, moved)
        return moved


SortKey = Literal["date", "amount", "description", "created"]
_SORT_COLUMNS: dict[str, str] = {
    "date": "t.txn_date",
    "amount": "t.amount_minor",
    "description": "t.normalized_description",
    "created": "t.created_at",
}


@dataclass(frozen=True, slots=True)
class TransactionFilter:
    """Filters for transaction queries. Voided (rolled-back) rows are excluded by default."""

    date_from: date | None = None
    date_to: date | None = None
    account_id: str | None = None
    currency: str | None = None
    category_id: str | None = None
    uncategorized: bool = False
    review_status: ReviewStatus | None = None
    merchant_key: str | None = None
    text: str | None = None
    min_amount_minor: int | None = None
    max_amount_minor: int | None = None
    batch_id: str | None = None
    include_voided: bool = False


@dataclass(frozen=True, slots=True)
class PageRequest:
    page: int = 1
    page_size: int = 50
    sort_by: SortKey = "date"
    descending: bool = True

    def __post_init__(self) -> None:
        if self.page < 1 or not 1 <= self.page_size <= 1000:
            raise ValueError("page must be >= 1 and page_size between 1 and 1000")
        if self.sort_by not in _SORT_COLUMNS:
            raise ValueError("unsupported sort key")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):  # noqa: UP046 - keep explicit Generic for broad tooling support
    items: list[T]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class MonthlyTotal:
    """Raw credits and debits for one month and currency (transfers included)."""

    month: str
    currency: str
    credits: Money
    debits: Money  # negative or zero
    count: int


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TransactionRepository:
    def __init__(self, conn: sqlite3.Connection, audit: AuditRepository | None = None) -> None:
        self._conn = conn
        self._audit = audit or AuditRepository(conn)

    @staticmethod
    def _row(row: sqlite3.Row) -> CanonicalTransaction:
        return CanonicalTransaction(
            id=row["id"],
            account_id=row["account_id"],
            import_batch_id=row["import_batch_id"],
            txn_date=date.fromisoformat(row["txn_date"]),
            booking_date=(date.fromisoformat(row["booking_date"]) if row["booking_date"] else None),
            original_description=row["original_description"],
            normalized_description=row["normalized_description"],
            merchant_key=row["merchant_key"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            reported_balance_minor=row["reported_balance_minor"],
            txn_type=row["txn_type"],
            category_id=row["category_id"],
            category_source=row["category_source"],
            review_status=row["review_status"],
            transfer_group_id=row["transfer_group_id"],
            source_ref=SourceRef.model_validate_json(row["source_ref_json"]),
            occurrence_index=row["occurrence_index"],
            is_voided=bool(row["is_voided"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, txn_id: str) -> CanonicalTransaction:
        row = self._conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if row is None:
            raise NotFoundError("transaction not found")
        return self._row(row)

    def find_matching(
        self,
        *,
        account_id: str,
        txn_date: date,
        amount_minor: int,
        currency: str,
        normalized_description: str,
    ) -> list[str]:
        """Ids of live (not rolled back) transactions identical on the duplicate-check key."""
        rows = self._conn.execute(
            "SELECT id FROM transactions WHERE is_voided = 0 AND account_id = ? AND txn_date = ? "
            "AND amount_minor = ? AND currency = ? AND normalized_description = ? "
            "ORDER BY occurrence_index, created_at, id",
            (account_id, txn_date.isoformat(), amount_minor, currency, normalized_description),
        ).fetchall()
        return [r["id"] for r in rows]

    @staticmethod
    def _where(flt: TransactionFilter) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if not flt.include_voided:
            clauses.append("t.is_voided = 0")
        if flt.date_from:
            clauses.append("t.txn_date >= ?")
            params.append(flt.date_from.isoformat())
        if flt.date_to:
            clauses.append("t.txn_date <= ?")
            params.append(flt.date_to.isoformat())
        for column, value in (
            ("t.account_id", flt.account_id),
            ("t.currency", flt.currency),
            ("t.category_id", flt.category_id),
            ("t.merchant_key", flt.merchant_key),
            ("t.import_batch_id", flt.batch_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if flt.uncategorized:
            clauses.append("t.category_id IS NULL")
        if flt.review_status is not None:
            clauses.append("t.review_status = ?")
            params.append(flt.review_status.value)
        if flt.min_amount_minor is not None:
            clauses.append("t.amount_minor >= ?")
            params.append(flt.min_amount_minor)
        if flt.max_amount_minor is not None:
            clauses.append("t.amount_minor <= ?")
            params.append(flt.max_amount_minor)
        if flt.text:
            like = f"%{_escape_like(flt.text.strip())}%"
            clauses.append(
                "(t.original_description LIKE ? ESCAPE '\\' "
                "OR t.normalized_description LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def query(
        self, flt: TransactionFilter | None = None, page: PageRequest | None = None
    ) -> Page[CanonicalTransaction]:
        flt = flt or TransactionFilter()
        page = page or PageRequest()
        where, params = self._where(flt)
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM transactions t{where}", params
        ).fetchone()[0]
        direction = "DESC" if page.descending else "ASC"
        order = f"{_SORT_COLUMNS[page.sort_by]} {direction}, t.id ASC"
        rows = self._conn.execute(
            f"SELECT t.* FROM transactions t{where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, page.page_size, (page.page - 1) * page.page_size],
        ).fetchall()
        return Page(
            items=[self._row(r) for r in rows],
            total=int(total),
            page=page.page,
            page_size=page.page_size,
        )

    def set_category_manual(self, txn_id: str, category_id: str) -> None:
        """Record a user's category choice. It is approved and never overwritten by automation."""
        self.get(txn_id)
        if not self._conn.execute(
            "SELECT 1 FROM categories WHERE id = ?", (category_id,)
        ).fetchone():
            raise NotFoundError("category not found")
        self._conn.execute(
            "UPDATE transactions SET category_id = ?, category_source = ?, review_status = ?, "
            "updated_at = ? WHERE id = ?",
            (
                category_id,
                CategorySource.USER_MANUAL.value,
                ReviewStatus.APPROVED.value,
                utc_now_iso(),
                txn_id,
            ),
        )

    def apply_automated_category(
        self,
        txn_id: str,
        category_id: str,
        source: CategorySource,
        review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW,
    ) -> bool:
        """Set a rule- or AI-derived category only on rows that have none.

        Returns False (and changes nothing) if the row is already categorized or approved, so
        user choices are never silently overwritten.
        """
        if source in (CategorySource.NONE, CategorySource.USER_MANUAL):
            raise ValueError("automated categorization needs a rule, mapping or llm source")
        cursor = self._conn.execute(
            "UPDATE transactions SET category_id = ?, category_source = ?, review_status = ?, "
            "updated_at = ? WHERE id = ? AND category_source = 'none' "
            "AND review_status != 'approved' AND is_voided = 0",
            (category_id, source.value, review_status.value, utc_now_iso(), txn_id),
        )
        return cursor.rowcount == 1

    def monthly_totals(
        self, *, account_id: str | None = None, include_voided: bool = False
    ) -> list[MonthlyTotal]:
        """Raw credits and debits per month and currency, never mixing currencies."""
        clauses = [] if include_voided else ["is_voided = 0"]
        params: list[object] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            "SELECT substr(txn_date, 1, 7) AS month, currency, "
            "SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END) AS credits, "
            "SUM(CASE WHEN amount_minor < 0 THEN amount_minor ELSE 0 END) AS debits, "
            f"COUNT(*) AS n FROM transactions{where} GROUP BY month, currency "
            "ORDER BY month, currency",
            params,
        ).fetchall()
        return [
            MonthlyTotal(
                month=r["month"],
                currency=r["currency"],
                credits=Money(int(r["credits"]), r["currency"]),
                debits=Money(int(r["debits"]), r["currency"]),
                count=int(r["n"]),
            )
            for r in rows
        ]


class ImportRepository:
    """Import batches: atomic commit, rollback (soft) and purge (hard delete)."""

    def __init__(self, conn: sqlite3.Connection, audit: AuditRepository | None = None) -> None:
        self._conn = conn
        self._audit = audit or AuditRepository(conn)

    @staticmethod
    def _row(row: sqlite3.Row) -> ImportBatch:
        return ImportBatch(
            id=row["id"],
            account_id=row["account_id"],
            file_sha256=row["file_sha256"],
            file_kind=row["file_kind"],
            display_name=row["display_name"],
            row_count_read=row["row_count_read"],
            row_count_imported=row["row_count_imported"],
            row_count_skipped=row["row_count_skipped"],
            warnings=tuple(json.loads(row["warnings_json"] or "[]")),
            status=BatchStatus(row["status"]),
            created_at=row["created_at"],
            rolled_back_at=row["rolled_back_at"],
        )

    def get(self, batch_id: str) -> ImportBatch:
        row = self._conn.execute(
            "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("import batch not found")
        return self._row(row)

    def list_batches(self) -> list[ImportBatch]:
        rows = self._conn.execute(
            "SELECT * FROM import_batches ORDER BY created_at DESC"
        ).fetchall()
        return [self._row(r) for r in rows]

    def find_by_hash(self, file_sha256: str) -> list[ImportBatch]:
        """Earlier committed imports of byte-identical files, to warn about re-imports."""
        rows = self._conn.execute(
            "SELECT * FROM import_batches WHERE file_sha256 = ? AND status = 'committed' "
            "ORDER BY created_at DESC",
            (file_sha256,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def commit_import(
        self, batch: ImportBatch, transactions: tuple[CanonicalTransaction, ...]
    ) -> ImportSummary:
        """Save a batch and its transactions atomically; any failure leaves nothing behind."""
        for txn in transactions:
            if txn.import_batch_id != batch.id or txn.account_id != batch.account_id:
                raise ValueError("transaction does not belong to this batch and account")
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO import_batches (id, account_id, file_sha256, file_kind, display_name, "
                "row_count_read, row_count_imported, row_count_skipped, warnings_json, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?)",
                (
                    batch.id,
                    batch.account_id,
                    batch.file_sha256,
                    batch.file_kind.value,
                    batch.display_name,
                    batch.row_count_read,
                    len(transactions),
                    batch.row_count_skipped,
                    json.dumps(list(batch.warnings)),
                    batch.created_at,
                ),
            )
            self._conn.executemany(
                "INSERT INTO transactions (id, account_id, import_batch_id, txn_date, booking_date, "
                "original_description, normalized_description, merchant_key, amount_minor, currency, "
                "reported_balance_minor, txn_type, category_id, category_source, review_status, "
                "transfer_group_id, source_ref_json, occurrence_index, is_voided, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        t.id,
                        t.account_id,
                        t.import_batch_id,
                        t.txn_date.isoformat(),
                        t.booking_date.isoformat() if t.booking_date else None,
                        t.original_description,
                        t.normalized_description,
                        t.merchant_key,
                        t.amount_minor,
                        t.currency,
                        t.reported_balance_minor,
                        t.txn_type.value,
                        t.category_id,
                        t.category_source.value,
                        t.review_status.value,
                        t.transfer_group_id,
                        t.source_ref.model_dump_json(),
                        t.occurrence_index,
                        1 if t.is_voided else 0,
                        t.created_at,
                        t.updated_at,
                    )
                    for t in transactions
                ],
            )
            self._audit.record("import_created", batch.id, len(transactions))
        return ImportSummary(
            batch_id=batch.id,
            imported=len(transactions),
            skipped=batch.row_count_skipped,
            warnings=batch.warnings,
        )

    def rollback(self, batch_id: str) -> RollbackSummary:
        """Void every transaction of a batch. Rows are kept for traceability but excluded
        from all queries and totals. Other batches are untouched."""
        batch = self.get(batch_id)
        if batch.status != BatchStatus.COMMITTED:
            raise InvalidStateError("this import was already rolled back")
        with transaction(self._conn):
            voided = self._conn.execute(
                "UPDATE transactions SET is_voided = 1, updated_at = ? "
                "WHERE import_batch_id = ? AND is_voided = 0",
                (utc_now_iso(), batch_id),
            ).rowcount
            self._conn.execute(
                "UPDATE import_batches SET status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
                (utc_now_iso(), batch_id),
            )
            self._audit.record("import_rolled_back", batch_id, voided)
        return RollbackSummary(batch_id=batch_id, voided=voided)

    def purge(self, batch_id: str) -> int:
        """Permanently delete a rolled-back batch and its rows. Returns rows deleted."""
        batch = self.get(batch_id)
        if batch.status != BatchStatus.ROLLED_BACK:
            raise InvalidStateError("only a rolled-back import can be purged")
        with transaction(self._conn):
            self._conn.execute(
                "DELETE FROM anomaly_alerts WHERE transaction_id IN "
                "(SELECT id FROM transactions WHERE import_batch_id = ?)",
                (batch_id,),
            )
            deleted = self._conn.execute(
                "DELETE FROM transactions WHERE import_batch_id = ?", (batch_id,)
            ).rowcount
            self._conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
            self._audit.record("import_purged", batch_id, deleted)
        return deleted
