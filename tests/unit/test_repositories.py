from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bank_analyzer.database.repositories import (
    AccountRepository,
    AuditRepository,
    CategoryRepository,
    ImportRepository,
    PageRequest,
    SettingsRepository,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.errors import InvalidStateError, NotFoundError
from bank_analyzer.domain.models import (
    Account,
    AccountType,
    BatchStatus,
    CanonicalTransaction,
    CategoryKind,
    CategorySource,
    FileKind,
    ImportBatch,
    ReviewStatus,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.domain.money import Money


def make_txn(
    account: Account,
    batch_id: str,
    *,
    day: str = "2024-01-15",
    amount: int = -1000,
    desc: str = "COFFEE SHOP",
    currency: str = "USD",
    row: int = 1,
) -> CanonicalTransaction:
    now = utc_now_iso()
    return CanonicalTransaction(
        id=new_id(),
        account_id=account.id,
        import_batch_id=batch_id,
        txn_date=date.fromisoformat(day),
        original_description=desc,
        normalized_description=desc.upper(),
        merchant_key=desc.upper(),
        amount_minor=amount,
        currency=currency,
        source_ref=SourceRef(file_sha256="a" * 64, row_index=row),
        created_at=now,
        updated_at=now,
    )


def make_batch(account: Account, sha: str = "a" * 64) -> ImportBatch:
    return ImportBatch(
        id=new_id(),
        account_id=account.id,
        file_sha256=sha,
        file_kind=FileKind.CSV,
        display_name="synthetic.csv",
        row_count_read=0,
        row_count_imported=0,
        row_count_skipped=0,
        created_at=utc_now_iso(),
    )


def load(imports: ImportRepository, account: Account, specs: list[dict]) -> ImportBatch:
    batch = make_batch(account)
    txns = tuple(make_txn(account, batch.id, row=i, **s) for i, s in enumerate(specs, start=1))
    imports.commit_import(batch, txns)
    return batch


class TestAccounts:
    def test_create_get_list(self, accounts: AccountRepository) -> None:
        a = accounts.create("Main", AccountType.DEPOSIT, "USD", last4="1234")
        assert accounts.get(a.id) == a
        assert [x.alias for x in accounts.list()] == ["Main"]

    def test_duplicate_alias_rejected(self, accounts: AccountRepository) -> None:
        accounts.create("Main", AccountType.DEPOSIT, "USD")
        with pytest.raises(InvalidStateError):
            accounts.create("Main", AccountType.CREDIT_CARD, "USD")

    def test_missing_account(self, accounts: AccountRepository) -> None:
        with pytest.raises(NotFoundError):
            accounts.get("nope")

    def test_full_account_number_not_accepted_as_last4(self, accounts: AccountRepository) -> None:
        with pytest.raises(ValueError):
            accounts.create("Main", AccountType.DEPOSIT, "USD", last4="12345678")


class TestCategories:
    def test_create_rename_disable(self, categories: CategoryRepository) -> None:
        c = categories.create("Pets", CategoryKind.EXPENSE)
        assert categories.rename(c.id, "Pet care").name == "Pet care"
        categories.set_active(c.id, False)
        assert "Pet care" not in {x.name for x in categories.list()}
        assert "Pet care" in {x.name for x in categories.list(active_only=False)}

    def test_duplicate_name_rejected(self, categories: CategoryRepository) -> None:
        with pytest.raises(InvalidStateError):
            categories.create("Groceries", CategoryKind.EXPENSE)
        pets = categories.create("Pets", CategoryKind.EXPENSE)
        with pytest.raises(InvalidStateError):
            categories.rename(pets.id, "Dining")

    def test_merge_moves_transactions_and_disables_source(
        self,
        categories: CategoryRepository,
        txns: TransactionRepository,
        imports: ImportRepository,
        usd_account: Account,
        audit: AuditRepository,
    ) -> None:
        batch = load(imports, usd_account, [{}, {}])
        items = txns.query().items
        src = categories.get_by_name("Dining")
        dst = categories.get_by_name("Groceries")
        txns.set_category_manual(items[0].id, src.id)
        txns.set_category_manual(items[1].id, src.id)
        assert categories.merge(src.id, dst.id) == 2
        assert {t.category_id for t in txns.query(TransactionFilter(batch_id=batch.id)).items} == {
            dst.id
        }
        assert categories.get(src.id).is_active is False
        assert categories.get(src.id).merged_into_id == dst.id
        assert ("category_merged", src.id, 2) in audit.list()

    def test_merge_guards(self, categories: CategoryRepository) -> None:
        a = categories.get_by_name("Dining")
        b = categories.create("Temp", CategoryKind.EXPENSE)
        with pytest.raises(InvalidStateError):
            categories.merge(a.id, a.id)
        categories.set_active(b.id, False)
        with pytest.raises(InvalidStateError):
            categories.merge(a.id, b.id)


class TestImportCommit:
    def test_commit_and_read_back(
        self, imports: ImportRepository, txns: TransactionRepository, usd_account: Account
    ) -> None:
        batch = make_batch(usd_account)
        t = make_txn(usd_account, batch.id, amount=-450)
        summary = imports.commit_import(batch, (t,))
        assert summary.imported == 1
        loaded = txns.get(t.id)
        assert loaded == t
        assert loaded.amount == Money(-450, "USD")
        assert imports.get(batch.id).row_count_imported == 1

    def test_atomic_on_failure_leaves_nothing(
        self,
        conn: sqlite3.Connection,
        imports: ImportRepository,
        usd_account: Account,
    ) -> None:
        batch = make_batch(usd_account)
        good = make_txn(usd_account, batch.id)
        dup = good.model_copy()  # same primary key -> second insert violates PRIMARY KEY
        with pytest.raises(sqlite3.IntegrityError):
            imports.commit_import(batch, (good, dup))
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        assert not conn.in_transaction

    def test_foreign_transaction_rejected(
        self, imports: ImportRepository, usd_account: Account
    ) -> None:
        batch = make_batch(usd_account)
        with pytest.raises(ValueError):
            imports.commit_import(batch, (make_txn(usd_account, "other-batch"),))

    def test_unknown_account_rejected(
        self, imports: ImportRepository, usd_account: Account
    ) -> None:
        ghost = usd_account.model_copy(update={"id": "ghost"})
        batch = make_batch(ghost)
        with pytest.raises(sqlite3.IntegrityError):
            imports.commit_import(batch, (make_txn(ghost, batch.id),))

    def test_find_by_hash(self, imports: ImportRepository, usd_account: Account) -> None:
        batch = load(imports, usd_account, [{}])
        assert [b.id for b in imports.find_by_hash("a" * 64)] == [batch.id]
        assert imports.find_by_hash("b" * 64) == []

    def test_identical_legitimate_transactions_all_stored(
        self, imports: ImportRepository, txns: TransactionRepository, usd_account: Account
    ) -> None:
        load(imports, usd_account, [{}, {}, {}])
        assert txns.query().total == 3

    def test_audit_has_no_content(
        self, imports: ImportRepository, usd_account: Account, audit: AuditRepository
    ) -> None:
        batch = load(imports, usd_account, [{"desc": "SECRET SHOP"}])
        events = audit.list()
        assert events == [("import_created", batch.id, 1)]


class TestRollbackAndPurge:
    def test_rollback_voids_only_that_batch(
        self, imports: ImportRepository, txns: TransactionRepository, usd_account: Account
    ) -> None:
        keep = load(imports, usd_account, [{"amount": -100}])
        drop = load(imports, usd_account, [{"amount": -500}, {"amount": 700}])
        before = txns.monthly_totals()
        assert before[0].debits.minor == -600
        result = imports.rollback(drop.id)
        assert result.voided == 2
        after = txns.monthly_totals()
        assert [(m.credits.minor, m.debits.minor, m.count) for m in after] == [(0, -100, 1)]
        assert txns.query().total == 1
        assert txns.query(TransactionFilter(include_voided=True)).total == 3
        assert imports.get(drop.id).status == BatchStatus.ROLLED_BACK
        assert imports.get(keep.id).status == BatchStatus.COMMITTED

    def test_rollback_twice_rejected(self, imports: ImportRepository, usd_account: Account) -> None:
        batch = load(imports, usd_account, [{}])
        imports.rollback(batch.id)
        with pytest.raises(InvalidStateError):
            imports.rollback(batch.id)

    def test_rolled_back_hash_not_reported_as_duplicate_import(
        self, imports: ImportRepository, usd_account: Account
    ) -> None:
        batch = load(imports, usd_account, [{}])
        imports.rollback(batch.id)
        assert imports.find_by_hash("a" * 64) == []

    def test_purge_requires_rollback_and_deletes(
        self, conn: sqlite3.Connection, imports: ImportRepository, usd_account: Account
    ) -> None:
        batch = load(imports, usd_account, [{}, {}])
        with pytest.raises(InvalidStateError):
            imports.purge(batch.id)
        imports.rollback(batch.id)
        assert imports.purge(batch.id) == 2
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        with pytest.raises(NotFoundError):
            imports.get(batch.id)


class TestQuery:
    @pytest.fixture
    def loaded(self, imports: ImportRepository, usd_account: Account) -> ImportBatch:
        return load(
            imports,
            usd_account,
            [
                {"day": "2024-01-05", "amount": -1000, "desc": "COFFEE SHOP"},
                {"day": "2024-01-20", "amount": -2500, "desc": "GROCERY MART"},
                {"day": "2024-02-02", "amount": 300000, "desc": "SALARY"},
                {"day": "2024-02-10", "amount": -500, "desc": "100% JUICE_BAR", "currency": "EUR"},
            ],
        )

    def test_date_range(self, txns: TransactionRepository, loaded: ImportBatch) -> None:
        page = txns.query(TransactionFilter(date_from=date(2024, 1, 1), date_to=date(2024, 1, 31)))
        assert page.total == 2

    def test_currency_filter(self, txns: TransactionRepository, loaded: ImportBatch) -> None:
        assert txns.query(TransactionFilter(currency="EUR")).total == 1

    def test_text_search_and_wildcard_escaping(
        self, txns: TransactionRepository, loaded: ImportBatch
    ) -> None:
        assert txns.query(TransactionFilter(text="grocery")).total == 1
        assert txns.query(TransactionFilter(text="100%")).total == 1
        assert txns.query(TransactionFilter(text="%")).total == 1  # literal percent only
        assert txns.query(TransactionFilter(text="_")).total == 1  # literal underscore only

    def test_sql_injection_text_is_inert(
        self, txns: TransactionRepository, loaded: ImportBatch, conn: sqlite3.Connection
    ) -> None:
        page = txns.query(TransactionFilter(text="'; DROP TABLE transactions; --"))
        assert page.total == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4

    def test_amount_filter(self, txns: TransactionRepository, loaded: ImportBatch) -> None:
        assert txns.query(TransactionFilter(min_amount_minor=0)).total == 1
        assert txns.query(TransactionFilter(max_amount_minor=-2000)).total == 1

    def test_sorting_and_stable_pagination(
        self, txns: TransactionRepository, loaded: ImportBatch
    ) -> None:
        by_amount = txns.query(page=PageRequest(sort_by="amount", descending=False))
        assert [t.amount_minor for t in by_amount.items] == [-2500, -1000, -500, 300000]
        p1 = txns.query(page=PageRequest(page=1, page_size=3))
        p2 = txns.query(page=PageRequest(page=2, page_size=3))
        assert (len(p1.items), len(p2.items), p1.total) == (3, 1, 4)
        assert not {t.id for t in p1.items} & {t.id for t in p2.items}

    def test_invalid_page_request(self) -> None:
        with pytest.raises(ValueError):
            PageRequest(page=0)
        with pytest.raises(ValueError):
            PageRequest(page_size=5000)
        with pytest.raises(ValueError):
            PageRequest(sort_by="amount; DROP TABLE x")  # type: ignore[arg-type]

    def test_uncategorized_and_review_filters(
        self, txns: TransactionRepository, loaded: ImportBatch, categories: CategoryRepository
    ) -> None:
        first = txns.query().items[0]
        txns.set_category_manual(first.id, categories.get_by_name("Salary").id)
        assert txns.query(TransactionFilter(uncategorized=True)).total == 3
        assert txns.query(TransactionFilter(review_status=ReviewStatus.APPROVED)).total == 1


class TestCategorization:
    def test_manual_category_is_approved(
        self,
        txns: TransactionRepository,
        imports: ImportRepository,
        usd_account: Account,
        categories: CategoryRepository,
    ) -> None:
        load(imports, usd_account, [{}])
        t = txns.query().items[0]
        cat = categories.get_by_name("Dining")
        txns.set_category_manual(t.id, cat.id)
        after = txns.get(t.id)
        assert (after.category_id, after.category_source, after.review_status) == (
            cat.id,
            CategorySource.USER_MANUAL,
            ReviewStatus.APPROVED,
        )

    def test_unknown_category_rejected(
        self, txns: TransactionRepository, imports: ImportRepository, usd_account: Account
    ) -> None:
        load(imports, usd_account, [{}])
        with pytest.raises(NotFoundError):
            txns.set_category_manual(txns.query().items[0].id, "nope")

    def test_automation_never_overwrites_user_choice(
        self,
        txns: TransactionRepository,
        imports: ImportRepository,
        usd_account: Account,
        categories: CategoryRepository,
    ) -> None:
        load(imports, usd_account, [{}])
        t = txns.query().items[0]
        mine = categories.get_by_name("Dining")
        other = categories.get_by_name("Shopping")
        txns.set_category_manual(t.id, mine.id)
        assert not txns.apply_automated_category(t.id, other.id, CategorySource.LLM)
        assert txns.get(t.id).category_id == mine.id

    def test_automation_never_overwrites_earlier_automation(
        self,
        txns: TransactionRepository,
        imports: ImportRepository,
        usd_account: Account,
        categories: CategoryRepository,
    ) -> None:
        load(imports, usd_account, [{}])
        t = txns.query().items[0]
        a, b = categories.get_by_name("Dining"), categories.get_by_name("Shopping")
        assert txns.apply_automated_category(t.id, a.id, CategorySource.BUILTIN_RULE)
        assert not txns.apply_automated_category(t.id, b.id, CategorySource.LLM)
        assert txns.get(t.id).category_id == a.id

    def test_automated_source_validated(
        self, txns: TransactionRepository, categories: CategoryRepository
    ) -> None:
        with pytest.raises(ValueError):
            txns.apply_automated_category("x", "y", CategorySource.USER_MANUAL)

    def test_invalid_llm_category_cannot_corrupt_row(
        self,
        conn: sqlite3.Connection,
        txns: TransactionRepository,
        imports: ImportRepository,
        usd_account: Account,
    ) -> None:
        load(imports, usd_account, [{}])
        t = txns.query().items[0]
        with pytest.raises(sqlite3.IntegrityError):
            txns.apply_automated_category(t.id, "not-a-category", CategorySource.LLM)
        assert txns.get(t.id).category_id is None


class TestMonthlyTotals:
    def test_multiple_currencies_kept_separate(
        self, txns: TransactionRepository, imports: ImportRepository, usd_account: Account
    ) -> None:
        load(
            imports,
            usd_account,
            [
                {"amount": 10000, "currency": "USD"},
                {"amount": -2500, "currency": "USD"},
                {"amount": 5000, "currency": "EUR"},
            ],
        )
        got = {
            (m.month, m.currency): (m.credits.minor, m.debits.minor) for m in txns.monthly_totals()
        }
        assert got == {("2024-01", "EUR"): (5000, 0), ("2024-01", "USD"): (10000, -2500)}

    def test_month_boundaries(
        self, txns: TransactionRepository, imports: ImportRepository, usd_account: Account
    ) -> None:
        load(
            imports,
            usd_account,
            [
                {"day": "2024-01-31", "amount": -100},
                {"day": "2024-02-01", "amount": -200},
                {"day": "2024-02-29", "amount": -300},
                {"day": "2024-03-01", "amount": -400},
            ],
        )
        got = [(m.month, m.debits.minor) for m in txns.monthly_totals()]
        assert got == [("2024-01", -100), ("2024-02", -500), ("2024-03", -400)]

    def test_account_filter(
        self,
        txns: TransactionRepository,
        imports: ImportRepository,
        usd_account: Account,
        accounts: AccountRepository,
    ) -> None:
        other = accounts.create("Savings", AccountType.DEPOSIT, "USD")
        load(imports, usd_account, [{"amount": -100}])
        load(imports, other, [{"amount": -900}])
        only = txns.monthly_totals(account_id=other.id)
        assert [m.debits.minor for m in only] == [-900]


class TestSettings:
    def test_get_set_overwrite(self, conn: sqlite3.Connection) -> None:
        s = SettingsRepository(conn)
        assert s.get("k") is None and s.get("k", "d") == "d"
        s.set("k", "1")
        s.set("k", "2")
        assert s.get("k") == "2"
