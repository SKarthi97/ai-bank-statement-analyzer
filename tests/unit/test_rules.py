from __future__ import annotations

import time
from typing import Any

import pytest

from bank_analyzer.domain.models import (
    AmountSign,
    CanonicalTransaction,
    CategorizationRule,
    MatchType,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.services.normalization import merchant_key, normalize_description
from bank_analyzer.services.rules import compile_rule, validate_rule_pattern


def txn(desc: str, amount: int = -1000) -> CanonicalTransaction:
    normalized = normalize_description(desc)
    now = utc_now_iso()
    return CanonicalTransaction(
        id=new_id(),
        account_id="acct-9f3b",
        import_batch_id="batch-77c1",
        txn_date="2024-01-01",  # type: ignore[arg-type]
        original_description=desc,
        normalized_description=normalized,
        merchant_key=merchant_key(normalized),
        amount_minor=amount,
        currency="USD",
        source_ref=SourceRef(file_sha256="0" * 64, row_index=1),
        created_at=now,
        updated_at=now,
    )


def rule(match_type: MatchType, pattern: str, sign: AmountSign | None = None) -> CategorizationRule:
    return CategorizationRule(
        id=new_id(),
        priority=10,
        match_type=match_type,
        pattern=pattern,
        amount_sign=sign,
        category_id="cat",
        created_at=utc_now_iso(),
    )


def matches(r: CategorizationRule, t: CanonicalTransaction) -> bool:
    compiled = compile_rule(r)
    assert compiled is not None
    return compiled.matches(t)


class TestPatternValidation:
    @pytest.mark.parametrize("pattern", ["", "   "])
    def test_empty_rejected(self, pattern: str) -> None:
        for kind in MatchType:
            with pytest.raises(ValueError, match="empty"):
                validate_rule_pattern(kind, pattern)

    def test_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="longer than"):
            validate_rule_pattern(MatchType.CONTAINS, "x" * 201)

    def test_text_rule_needs_searchable_text(self) -> None:
        with pytest.raises(ValueError, match="searchable"):
            validate_rule_pattern(MatchType.CONTAINS, "###")

    @pytest.mark.parametrize(
        "pattern",
        ["(a+)+$", "(a|aa)+b", "(a*)*", r"(ab){2,}", "(.*)+x"],
    )
    def test_quantified_groups_rejected(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="group"):
            validate_rule_pattern(MatchType.REGEX, pattern)

    def test_backreferences_rejected(self) -> None:
        with pytest.raises(ValueError, match="Back-references"):
            validate_rule_pattern(MatchType.REGEX, r"(ab)\1")

    @pytest.mark.parametrize("pattern", ["a*a*b", ".*.*x", "^a*a*a*b", ".{1,500}.{1,500}", "a+b+"])
    def test_too_many_heavy_repeats_rejected(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="at most one"):
            validate_rule_pattern(MatchType.REGEX, pattern)

    @pytest.mark.parametrize(
        "pattern", ["^a*a*b", "^UBER.*TRIP.*END", r"\d{4}-\d{2}-\d{2}", "a{1,5}b{1,5}"]
    )
    def test_anchored_or_small_bounded_repeats_accepted(self, pattern: str) -> None:
        assert validate_rule_pattern(MatchType.REGEX, pattern) == pattern

    def test_invalid_regex_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            validate_rule_pattern(MatchType.REGEX, "[unclosed")

    @pytest.mark.parametrize(
        "pattern", [r"^NETFLIX\b", r"UBER.*TRIP", r"\d{4}-\d{2}", "GYM|FITNESS"]
    )
    def test_reasonable_regexes_accepted(self, pattern: str) -> None:
        assert validate_rule_pattern(MatchType.REGEX, pattern) == pattern

    def test_escaped_star_is_not_an_unbounded_repeat(self) -> None:
        validate_rule_pattern(MatchType.REGEX, r"A\*B\*C\*D")

    @pytest.mark.parametrize("pattern", ["a*b", "^a*a*b", ".*x", "^.*a.*b"])
    def test_every_accepted_regex_is_fast_on_worst_case_input(self, pattern: str) -> None:
        validate_rule_pattern(MatchType.REGEX, pattern)
        compiled = compile_rule(rule(MatchType.REGEX, pattern))
        assert compiled is not None
        subject = txn("a" * 1000)  # descriptions are capped at 1000 characters
        start = time.perf_counter()
        assert not compiled.matches(subject)
        assert time.perf_counter() - start < 0.5

    def test_stored_invalid_pattern_is_not_compiled(self) -> None:
        assert compile_rule(rule(MatchType.REGEX, "(a+)+$")) is None


class TestMatching:
    def test_contains_uses_normalised_text(self) -> None:
        r = rule(MatchType.CONTAINS, "netflix.com")
        assert matches(r, txn("NETFLIX.COM 12345 CA"))
        assert matches(r, txn("netflix com"))
        assert not matches(r, txn("NETFLIX"))

    def test_starts_with(self) -> None:
        r = rule(MatchType.STARTS_WITH, "pos purchase")
        assert matches(r, txn("POS PURCHASE STARBUCKS"))
        assert not matches(r, txn("STARBUCKS POS PURCHASE"))

    def test_merchant_key_is_exact(self) -> None:
        r = rule(MatchType.MERCHANT_KEY, "starbucks")
        assert matches(r, txn("POS PURCHASE STARBUCKS #55"))
        assert not matches(r, txn("STARBUCKS RESERVE ROASTERY"))  # key differs

    def test_regex_runs_on_the_original_description_case_insensitively(self) -> None:
        r = rule(MatchType.REGEX, r"^trip \d{3}$")
        assert matches(r, txn("TRIP 123"))
        assert not matches(r, txn("TRIP 12"))

    @pytest.mark.parametrize(
        ("sign", "amount", "expected"),
        [
            (AmountSign.IN, 500, True),
            (AmountSign.IN, -500, False),
            (AmountSign.IN, 0, False),
            (AmountSign.OUT, -500, True),
            (AmountSign.OUT, 500, False),
            (None, 500, True),
            (None, -500, True),
        ],
    )
    def test_direction_filter(self, sign: AmountSign | None, amount: int, expected: bool) -> None:
        r = rule(MatchType.CONTAINS, "payment", sign)
        assert matches(r, txn("PAYMENT RECEIVED", amount)) is expected

    def test_rule_model_rejects_oversized_pattern(self) -> None:
        kwargs: dict[str, Any] = {
            "id": "x",
            "priority": 1,
            "match_type": MatchType.CONTAINS,
            "pattern": "x" * 201,
            "category_id": "c",
            "created_at": "now",
        }
        with pytest.raises(ValueError):
            CategorizationRule(**kwargs)
