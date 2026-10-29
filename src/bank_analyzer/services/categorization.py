"""Hybrid categorization: deterministic rules first, a local model only as a last resort.

Precedence for an uncategorized transaction:

1. user-defined rules (priority order, first match wins)
2. merchant mappings the user previously approved
3. built-in merchant/keyword rules
4. the local model, if one is running
5. otherwise it stays uncategorized and waits for the user (the "Needs Review" state)

Guarantees:

* A category the user set or approved is never changed by automation, and a suggestion the user
  rejected is not proposed again.
* Model output is validated item by item (see ``ai.response_validator``); an invalid item leaves
  the row untouched. A model suggestion is always stored as *unverified* (``needs_review``) and a
  merchant mapping is only ever created by an explicit user action.
* An income category is never accepted from a model for money going out, so a hostile
  description cannot turn a payment into "Salary".
* With no model (or a failing one) rules still run and the run reports it; nothing breaks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from bank_analyzer.ai.local_llm import LLMError, LLMUnavailable, LocalLLM
from bank_analyzer.ai.prompts import (
    build_classification_messages,
    classification_schema,
    looks_like_injection,
    minimal_item,
)
from bank_analyzer.ai.response_validator import Verdict, validate_classification
from bank_analyzer.database.connection import transaction
from bank_analyzer.database.repositories import (
    AuditRepository,
    CategorizationRuleRepository,
    CategoryRepository,
    MerchantMappingRepository,
    Page,
    PageRequest,
    TransactionFilter,
    TransactionRepository,
)
from bank_analyzer.domain.models import (
    AmountSign,
    CanonicalTransaction,
    CategorizationRule,
    Category,
    CategoryKind,
    CategorySource,
    MatchType,
    ReviewStatus,
)
from bank_analyzer.services.builtin_rules import CompiledBuiltin, compile_builtins
from bank_analyzer.services.rules import CompiledRule, compile_rule, validate_rule_pattern

# Shown next to a category in the UI. Model output is never presented as a probability.
SOURCE_LABELS: dict[CategorySource, str] = {
    CategorySource.NONE: "Uncategorized",
    CategorySource.USER_MANUAL: "Set by you",
    CategorySource.USER_RULE: "Your rule",
    CategorySource.MERCHANT_MAPPING: "Approved merchant",
    CategorySource.BUILTIN_RULE: "Built-in rule",
    CategorySource.LLM: "AI suggestion (unverified)",
}


@dataclass(frozen=True, slots=True)
class Suggestion:
    category_id: str
    source: CategorySource
    review_status: ReviewStatus


class RuleEngine:
    """The deterministic part of the cascade. Pure: no database access."""

    def __init__(
        self,
        rules: list[CompiledRule],
        mappings: dict[str, str],
        builtins: list[tuple[CompiledBuiltin, str]],
    ) -> None:
        self._rules = rules
        self._mappings = mappings
        self._builtins = builtins

    def suggest(self, txn: CanonicalTransaction) -> Suggestion | None:
        for compiled in self._rules:
            if compiled.matches(txn):
                return Suggestion(
                    compiled.rule.category_id, CategorySource.USER_RULE, ReviewStatus.OK
                )
        if txn.merchant_key and txn.merchant_key in self._mappings:
            return Suggestion(
                self._mappings[txn.merchant_key], CategorySource.MERCHANT_MAPPING, ReviewStatus.OK
            )
        for builtin, category_id in self._builtins:
            if builtin.matches(txn):
                status = ReviewStatus.NEEDS_REVIEW if builtin.rule.weak else ReviewStatus.OK
                return Suggestion(category_id, CategorySource.BUILTIN_RULE, status)
        return None


@dataclass(slots=True)
class CategorizationReport:
    """What a run did. Counts only: no statement content."""

    total_pending: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    llm_attempted: bool = False
    llm_unavailable: bool = False
    llm_abstained: int = 0  # the model said it was unsure
    llm_invalid: int = 0  # malformed, missing, duplicated or out-of-enum items (ignored)
    llm_rejected_inconsistent: int = 0  # e.g. an income category for money going out
    suspicious_descriptions: int = 0  # descriptions that look like instructions to a model
    remaining_uncategorized: int = 0

    @property
    def applied(self) -> int:
        return sum(self.by_source.values())


@dataclass(frozen=True, slots=True)
class ReviewResult:
    changed: int
    skipped: int
    mappings_saved: int = 0
    mapping_conflicts: int = 0
    newly_categorized: int = 0


class Categorizer:
    """Runs the cascade and provides the human-review operations."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        llm: LocalLLM | None = None,
        *,
        batch_size: int = 20,
        max_attempts: int = 2,
    ) -> None:
        self._conn = conn
        self._llm = llm
        self._batch_size = max(1, batch_size)
        self._max_attempts = max(1, max_attempts)
        audit = AuditRepository(conn)
        self._audit = audit
        self._txns = TransactionRepository(conn, audit)
        self._categories = CategoryRepository(conn, audit)
        self._rules = CategorizationRuleRepository(conn, audit)
        self._mappings = MerchantMappingRepository(conn, audit)

    # -- rules ---------------------------------------------------------------------------------

    def add_rule(
        self,
        *,
        match_type: MatchType,
        pattern: str,
        category_id: str,
        amount_sign: AmountSign | None = None,
        priority: int | None = None,
    ) -> CategorizationRule:
        """Validate and save a user rule. Raises ValueError with a user-facing reason."""
        clean = validate_rule_pattern(match_type, pattern)
        return self._rules.add(
            match_type=match_type,
            pattern=clean,
            category_id=category_id,
            amount_sign=amount_sign,
            priority=priority,
        )

    def _engine(self, categories: list[Category]) -> RuleEngine:
        active = {c.id for c in categories}
        by_name = {c.name: c.id for c in categories}
        rules = [
            compiled
            for rule in self._rules.list_rules()
            if rule.category_id in active and (compiled := compile_rule(rule)) is not None
        ]
        mappings = {
            m.merchant_key: m.category_id
            for m in self._mappings.list_mappings()
            if m.category_id in active
        }
        builtins = [
            (compiled, by_name[compiled.rule.category])
            for compiled in compile_builtins()
            if compiled.rule.category in by_name
        ]
        return RuleEngine(rules, mappings, builtins)

    # -- the cascade ---------------------------------------------------------------------------

    def run(self, *, batch_id: str | None = None, use_llm: bool = True) -> CategorizationReport:
        """Categorize pending transactions. Safe to repeat: protected rows are never touched."""
        pending = self._txns.list_pending_categorization(batch_id=batch_id)
        report = CategorizationReport(total_pending=len(pending))
        categories = self._categories.list(active_only=True)
        engine = self._engine(categories)

        leftovers: list[CanonicalTransaction] = []
        with transaction(self._conn):
            for txn in pending:
                suggestion = engine.suggest(txn)
                if suggestion is not None and self._txns.apply_automated_category(
                    txn.id, suggestion.category_id, suggestion.source, suggestion.review_status
                ):
                    key = suggestion.source.value
                    report.by_source[key] = report.by_source.get(key, 0) + 1
                else:
                    leftovers.append(txn)
            self._audit.record("categorization_rules_run", batch_id, len(pending) - len(leftovers))

        if use_llm and self._llm is not None and leftovers:
            self._run_llm(leftovers, categories, report)
        report.remaining_uncategorized = len(leftovers) - report.by_source.get("llm", 0)
        return report

    def _run_llm(
        self,
        leftovers: list[CanonicalTransaction],
        categories: list[Category],
        report: CategorizationReport,
    ) -> None:
        assert self._llm is not None
        report.llm_attempted = True
        by_name = {c.name: c for c in categories}
        allowed = [c.name for c in categories]
        schema = classification_schema(allowed)

        for start in range(0, len(leftovers), self._batch_size):
            chunk = leftovers[start : start + self._batch_size]
            items = [minimal_item(t, i) for i, t in enumerate(chunk)]
            report.suspicious_descriptions += sum(
                looks_like_injection(i.description) for i in items
            )
            system, user = build_classification_messages(items, allowed)

            outcome = None
            for _ in range(self._max_attempts):
                try:
                    raw = self._llm.chat_json(system, user, schema)
                except LLMUnavailable:
                    report.llm_unavailable = True
                    return  # the model is gone: rules already ran, the rest waits for the user
                except LLMError:
                    continue  # reachable but unusable reply: try again, then give up on the chunk
                outcome = validate_classification(
                    raw, requested=[i.index for i in items], allowed=allowed
                )
                if not outcome.structural_error:
                    break
            if outcome is None or outcome.structural_error:
                report.llm_invalid += len(chunk)
                continue

            with transaction(self._conn):
                for index, item in outcome.items.items():
                    if item.verdict == Verdict.ABSTAINED:
                        report.llm_abstained += 1
                    elif item.verdict == Verdict.INVALID:
                        report.llm_invalid += 1
                    else:
                        self._apply_llm_item(chunk[index], by_name[item.category or ""], report)

    def _apply_llm_item(
        self, txn: CanonicalTransaction, category: Category, report: CategorizationReport
    ) -> None:
        # An income category for money going out can only be a mistake or an attack.
        if category.kind == CategoryKind.INCOME and txn.amount_minor < 0:
            report.llm_rejected_inconsistent += 1
            return
        if self._txns.apply_automated_category(
            txn.id, category.id, CategorySource.LLM, ReviewStatus.NEEDS_REVIEW
        ):
            report.by_source["llm"] = report.by_source.get("llm", 0) + 1

    # -- human review --------------------------------------------------------------------------

    def review_queue(self, *, page: int = 1, page_size: int = 50) -> Page[CanonicalTransaction]:
        """Transactions waiting for a human: suggestions to approve and rows still uncategorized."""
        return self._txns.query(
            TransactionFilter(review_status=ReviewStatus.NEEDS_REVIEW),
            PageRequest(page=page, page_size=page_size, sort_by="date", descending=False),
        )

    def approve(
        self,
        txn_ids: list[str],
        *,
        remember_merchant: bool = True,
        apply_to_pending: bool = True,
    ) -> ReviewResult:
        """Approve automated categories in bulk (atomic).

        Approving an AI or built-in suggestion can remember the merchant, so future rows from it
        are categorized by the (approved) mapping. If one bulk approval assigns the same merchant
        to different categories, no mapping is saved for it. Existing mappings are overwritten
        because this is an explicit user action.
        """
        wanted: dict[str, set[str]] = {}
        changed = skipped = 0
        with transaction(self._conn):
            for txn_id in dict.fromkeys(txn_ids):
                txn = self._txns.get(txn_id)
                if txn.is_voided or not self._txns.approve_suggestion(txn_id):
                    skipped += 1
                    continue
                changed += 1
                if (
                    remember_merchant
                    and txn.merchant_key
                    and txn.category_id
                    and txn.category_source in (CategorySource.LLM, CategorySource.BUILTIN_RULE)
                ):
                    wanted.setdefault(txn.merchant_key, set()).add(txn.category_id)
            saved, conflicts = self._save_mappings(wanted)
            self._audit.record("categories_approved", None, changed)
        return self._finish(changed, skipped, saved, conflicts, apply_to_pending)

    def correct(
        self,
        txn_ids: list[str],
        category_id: str,
        *,
        remember_merchant: bool = True,
        apply_to_pending: bool = True,
    ) -> ReviewResult:
        """Set one category on many transactions as the user's decision (atomic)."""
        wanted: dict[str, set[str]] = {}
        changed = skipped = 0
        with transaction(self._conn):
            for txn_id in dict.fromkeys(txn_ids):
                txn = self._txns.get(txn_id)
                if txn.is_voided:
                    skipped += 1
                    continue
                self._txns.set_category_manual(txn_id, category_id)
                changed += 1
                if remember_merchant and txn.merchant_key:
                    wanted[txn.merchant_key] = {category_id}
            saved, conflicts = self._save_mappings(wanted)
            self._audit.record("categories_corrected", None, changed)
        return self._finish(changed, skipped, saved, conflicts, apply_to_pending)

    def reject_suggestions(self, txn_ids: list[str]) -> int:
        """Remove automated suggestions the user disagrees with; they are not proposed again."""
        rejected = 0
        with transaction(self._conn):
            for txn_id in dict.fromkeys(txn_ids):
                self._txns.get(txn_id)
                if self._txns.reject_suggestion(txn_id):
                    rejected += 1
            self._audit.record("suggestions_rejected", None, rejected)
        return rejected

    def _save_mappings(self, wanted: dict[str, set[str]]) -> tuple[int, int]:
        saved = conflicts = 0
        for merchant_key, categories in wanted.items():
            if len(categories) == 1:
                self._mappings.upsert(merchant_key, next(iter(categories)))
                saved += 1
            else:
                conflicts += 1
        return saved, conflicts

    def _finish(
        self, changed: int, skipped: int, saved: int, conflicts: int, apply_to_pending: bool
    ) -> ReviewResult:
        newly = self.run(use_llm=False).applied if saved and apply_to_pending else 0
        return ReviewResult(changed, skipped, saved, conflicts, newly)
