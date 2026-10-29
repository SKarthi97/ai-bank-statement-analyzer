from __future__ import annotations

import sqlite3

import pytest

from bank_analyzer.database.repositories import (
    AuditRepository,
    CategorizationRuleRepository,
    CategoryRepository,
    MerchantMappingRepository,
    TransactionRepository,
)
from bank_analyzer.domain.errors import InvalidStateError, NotFoundError
from bank_analyzer.domain.models import (
    Account,
    AmountSign,
    CategorySource,
    MatchType,
    ReviewStatus,
)
from fakes import load_transactions


@pytest.fixture
def rules(conn: sqlite3.Connection) -> CategorizationRuleRepository:
    return CategorizationRuleRepository(conn)


@pytest.fixture
def mappings(conn: sqlite3.Connection) -> MerchantMappingRepository:
    return MerchantMappingRepository(conn)


class TestRuleRepository:
    def test_add_list_in_priority_order(
        self, rules: CategorizationRuleRepository, categories: CategoryRepository
    ) -> None:
        dining = categories.get_by_name("Dining").id
        late = rules.add(
            match_type=MatchType.CONTAINS, pattern="b", category_id=dining, priority=50
        )
        early = rules.add(
            match_type=MatchType.STARTS_WITH,
            pattern="a",
            category_id=dining,
            amount_sign=AmountSign.OUT,
            priority=5,
        )
        assert [r.id for r in rules.list_rules()] == [early.id, late.id]
        assert rules.list_rules()[0].amount_sign == AmountSign.OUT

    def test_default_priority_goes_last(
        self, rules: CategorizationRuleRepository, categories: CategoryRepository
    ) -> None:
        dining = categories.get_by_name("Dining").id
        first = rules.add(match_type=MatchType.CONTAINS, pattern="a", category_id=dining)
        second = rules.add(match_type=MatchType.CONTAINS, pattern="b", category_id=dining)
        assert (first.priority, second.priority) == (10, 20)

    def test_unknown_category_rejected(self, rules: CategorizationRuleRepository) -> None:
        with pytest.raises(NotFoundError):
            rules.add(match_type=MatchType.CONTAINS, pattern="a", category_id="nope")

    def test_disable_reprioritise_delete(
        self, rules: CategorizationRuleRepository, categories: CategoryRepository
    ) -> None:
        dining = categories.get_by_name("Dining").id
        rule = rules.add(match_type=MatchType.CONTAINS, pattern="a", category_id=dining)
        rules.set_active(rule.id, False)
        assert rules.list_rules() == [] and len(rules.list_rules(active_only=False)) == 1
        rules.set_priority(rule.id, 1)
        assert rules.list_rules(active_only=False)[0].priority == 1
        rules.delete(rule.id)
        assert rules.list_rules(active_only=False) == []

    @pytest.mark.parametrize("action", ["active", "priority", "delete"])
    def test_missing_rule_raises(self, rules: CategorizationRuleRepository, action: str) -> None:
        with pytest.raises(NotFoundError):
            {
                "active": lambda: rules.set_active("x", True),
                "priority": lambda: rules.set_priority("x", 1),
                "delete": lambda: rules.delete("x"),
            }[action]()

    def test_changes_are_audited_without_the_pattern(
        self,
        rules: CategorizationRuleRepository,
        categories: CategoryRepository,
        audit: AuditRepository,
    ) -> None:
        dining = categories.get_by_name("Dining").id
        rules.add(match_type=MatchType.CONTAINS, pattern="SECRET-PATTERN", category_id=dining)
        assert "SECRET" not in str(audit.list())
        assert audit.list()[0][0] == "rule_added"

    def test_merging_a_category_moves_its_rules(
        self, rules: CategorizationRuleRepository, categories: CategoryRepository
    ) -> None:
        old = categories.create("Old", categories.get_by_name("Dining").kind)
        rule = rules.add(match_type=MatchType.CONTAINS, pattern="a", category_id=old.id)
        categories.merge(old.id, categories.get_by_name("Dining").id)
        assert rules.list_rules()[0].category_id == categories.get_by_name("Dining").id
        assert rules.list_rules()[0].id == rule.id


class TestMerchantMappings:
    def test_upsert_overwrites_and_lists(
        self, mappings: MerchantMappingRepository, categories: CategoryRepository
    ) -> None:
        mappings.upsert("NETFLIX", categories.get_by_name("Subscriptions").id)
        mappings.upsert("NETFLIX", categories.get_by_name("Entertainment").id)
        assert mappings.get("NETFLIX") == categories.get_by_name("Entertainment").id
        assert [m.merchant_key for m in mappings.list_mappings()] == ["NETFLIX"]
        assert mappings.get("OTHER") is None

    def test_unknown_category_and_missing_delete(self, mappings: MerchantMappingRepository) -> None:
        with pytest.raises(NotFoundError):
            mappings.upsert("X", "no-such-category")
        with pytest.raises(NotFoundError):
            mappings.delete("X")

    def test_delete(
        self, mappings: MerchantMappingRepository, categories: CategoryRepository
    ) -> None:
        mappings.upsert("X", categories.get_by_name("Dining").id)
        mappings.delete("X")
        assert mappings.get("X") is None


class TestTransactionTransitions:
    def test_pending_excludes_categorized_approved_rejected_and_voided(
        self,
        conn: sqlite3.Connection,
        usd_account: Account,
        txns: TransactionRepository,
        categories: CategoryRepository,
    ) -> None:
        rows = load_transactions(
            conn, usd_account.id, [{"desc": f"M{i}", "day": f"2024-01-0{i + 1}"} for i in range(6)]
        )
        dining = categories.get_by_name("Dining").id
        txns.set_category_manual(rows[0].id, dining)
        txns.apply_automated_category(rows[1].id, dining, CategorySource.LLM)
        txns.apply_automated_category(rows[2].id, dining, CategorySource.LLM)
        txns.reject_suggestion(rows[2].id)
        conn.execute("UPDATE transactions SET is_voided = 1 WHERE id = ?", (rows[3].id,))
        pending = {t.id for t in txns.list_pending_categorization()}
        assert pending == {rows[4].id, rows[5].id}
        assert [t.id for t in txns.list_pending_categorization(limit=1)] == [rows[4].id]

    def test_approve_and_reject_state_machine(
        self,
        conn: sqlite3.Connection,
        usd_account: Account,
        txns: TransactionRepository,
        categories: CategoryRepository,
    ) -> None:
        (row,) = load_transactions(conn, usd_account.id, [{"desc": "M"}])
        dining = categories.get_by_name("Dining").id
        assert not txns.approve_suggestion(row.id)  # nothing to approve yet
        assert not txns.reject_suggestion(row.id)
        txns.apply_automated_category(row.id, dining, CategorySource.LLM)
        assert txns.approve_suggestion(row.id) and not txns.approve_suggestion(row.id)
        assert not txns.reject_suggestion(row.id)  # approved rows are protected
        assert txns.get(row.id).review_status == ReviewStatus.APPROVED

    def test_manual_category_cannot_be_rejected(
        self,
        conn: sqlite3.Connection,
        usd_account: Account,
        txns: TransactionRepository,
        categories: CategoryRepository,
    ) -> None:
        (row,) = load_transactions(conn, usd_account.id, [{"desc": "M"}])
        txns.set_category_manual(row.id, categories.get_by_name("Dining").id)
        assert not txns.reject_suggestion(row.id)
        assert txns.get(row.id).category_source == CategorySource.USER_MANUAL

    def test_rejected_row_stays_out_of_automation_but_user_can_still_categorize(
        self,
        conn: sqlite3.Connection,
        usd_account: Account,
        txns: TransactionRepository,
        categories: CategoryRepository,
    ) -> None:
        (row,) = load_transactions(conn, usd_account.id, [{"desc": "M"}])
        dining = categories.get_by_name("Dining").id
        txns.apply_automated_category(row.id, dining, CategorySource.LLM)
        txns.reject_suggestion(row.id)
        assert not txns.apply_automated_category(row.id, dining, CategorySource.LLM)
        txns.set_category_manual(row.id, dining)  # the human may always decide
        assert txns.get(row.id).category_source == CategorySource.USER_MANUAL

    def test_manual_category_must_be_active(
        self,
        conn: sqlite3.Connection,
        usd_account: Account,
        txns: TransactionRepository,
        categories: CategoryRepository,
    ) -> None:
        (row,) = load_transactions(conn, usd_account.id, [{"desc": "M"}])
        dining = categories.get_by_name("Dining")
        categories.set_active(dining.id, False)
        with pytest.raises(InvalidStateError, match="disabled"):
            txns.set_category_manual(row.id, dining.id)
        with pytest.raises(NotFoundError):
            txns.set_category_manual(row.id, "missing")
