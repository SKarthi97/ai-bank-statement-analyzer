"""User-defined categorization rules: validation and matching.

``contains``, ``starts_with`` and ``merchant_key`` rules compare against the normalized
description (so ``NETFLIX.COM`` and ``netflix com`` are equivalent). ``regex`` rules run against
the original description, case-insensitively.

User regexes are a denial-of-service risk because Python's ``re`` has no timeout. Defence in
depth, not a proof: patterns are length-capped, must not quantify groups or use backreferences,
may use only one heavy repeat (two when anchored with '^'), and always run on text capped at
1,000 characters. The limit comes from measurement: with two unanchored repeats a search is
cubic in the text length (about 2 s at 1,000 characters), and every extra repeat adds a power.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bank_analyzer.domain.models import (
    AmountSign,
    CanonicalTransaction,
    CategorizationRule,
    MatchType,
)
from bank_analyzer.services.normalization import normalize_description

MAX_PATTERN_LENGTH = 200
# 'Heavy' repeats are * + {n,} and {n,m} with m > 20. One is cheap anywhere; a second is only
# cheap when the pattern is anchored at the start, because an unanchored search retries at
# every position.
MAX_HEAVY_REPEATS = 1
MAX_HEAVY_REPEATS_ANCHORED = 2
_BIG_REPEAT = 20

_QUANTIFIED_GROUP = re.compile(r"\)\s*(?:[*+]|\{)")
_BACKREFERENCE = re.compile(r"\\[1-9]|\(\?P=|\\k<")
_STAR_PLUS = re.compile(r"(?<!\\)[*+]")
_BRACES = re.compile(r"(?<!\\)\{(\d*)(,?)(\d*)\}")


def _heavy_repeats(pattern: str) -> int:
    count = len(_STAR_PLUS.findall(pattern))
    for low, comma, high in _BRACES.findall(pattern):
        upper = high if comma else low
        if (comma and not high) or (upper and int(upper) > _BIG_REPEAT):
            count += 1
    return count


def validate_rule_pattern(match_type: MatchType, pattern: str) -> str:
    """Return the pattern to store, or raise ValueError with a user-facing reason."""
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("The pattern is empty.")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"The pattern is longer than {MAX_PATTERN_LENGTH} characters.")
    if match_type == MatchType.REGEX:
        if _QUANTIFIED_GROUP.search(pattern):
            raise ValueError("Repeating a group (such as '(ab)+') is not allowed in rules.")
        if _BACKREFERENCE.search(pattern):
            raise ValueError("Back-references are not allowed in rules.")
        limit = MAX_HEAVY_REPEATS_ANCHORED if pattern.startswith("^") else MAX_HEAVY_REPEATS
        if _heavy_repeats(pattern) > limit:
            raise ValueError(
                "Use at most one '*' or '+' repeat in a pattern (two if it starts with '^')."
            )
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("The pattern is not a valid regular expression.") from exc
        return pattern
    if not normalize_description(pattern):
        raise ValueError("The pattern contains no searchable text.")
    return pattern


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A rule prepared for matching."""

    rule: CategorizationRule
    needle: str
    regex: re.Pattern[str] | None

    def matches(self, txn: CanonicalTransaction) -> bool:
        sign = self.rule.amount_sign
        if sign == AmountSign.IN and txn.amount_minor <= 0:
            return False
        if sign == AmountSign.OUT and txn.amount_minor >= 0:
            return False
        kind = self.rule.match_type
        if kind == MatchType.CONTAINS:
            return self.needle in txn.normalized_description
        if kind == MatchType.STARTS_WITH:
            return txn.normalized_description.startswith(self.needle)
        if kind == MatchType.MERCHANT_KEY:
            return txn.merchant_key == self.needle
        return self.regex is not None and self.regex.search(txn.original_description) is not None


def compile_rule(rule: CategorizationRule) -> CompiledRule | None:
    """Prepare a stored rule. Returns None if its pattern no longer passes validation."""
    try:
        validate_rule_pattern(rule.match_type, rule.pattern)
    except ValueError:
        return None
    if rule.match_type == MatchType.REGEX:
        return CompiledRule(rule, "", re.compile(rule.pattern, re.IGNORECASE))
    return CompiledRule(rule, normalize_description(rule.pattern), None)
