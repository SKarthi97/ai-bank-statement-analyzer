"""Strict validation of model output. Nothing from a model is trusted until it passes here.

A reply is accepted per item, never as a whole by faith: a malformed, duplicated, unexpected or
out-of-enum item is rejected on its own and the row simply stays for human review. Categories
must match an allowed name exactly (no fuzzy matching, no case folding), and free text is
sanitised and length-capped because it is displayed to the user.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from bank_analyzer.ai.prompts import MAX_REASON_CHARS, NEEDS_REVIEW
from bank_analyzer.domain.validation import sanitize_text


class Verdict(StrEnum):
    ACCEPTED = "accepted"  # a valid category from the allowed set
    ABSTAINED = "abstained"  # the model said it was unsure: leave for review
    INVALID = "invalid"  # missing, duplicated, malformed or out-of-enum: ignore


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    index: int
    verdict: Verdict
    category: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ClassificationOutcome:
    items: dict[int, ItemOutcome]
    structural_error: bool  # the reply was not the expected object at all
    unexpected_items: int  # entries ignored: bad shape or an index that was not requested

    def count(self, verdict: Verdict) -> int:
        return sum(1 for o in self.items.values() if o.verdict == verdict)


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    i: int
    category: str
    needs_review: bool
    reason: str = ""


def _clean_reason(text: str) -> str:
    cleaned, _ = sanitize_text(text)
    return cleaned[:MAX_REASON_CHARS]


def validate_classification(
    raw: object, *, requested: Sequence[int], allowed: Collection[str]
) -> ClassificationOutcome:
    """Validate a parsed model reply against the indices requested and the allowed categories."""
    invalid_all = ClassificationOutcome(
        items={i: ItemOutcome(i, Verdict.INVALID) for i in requested},
        structural_error=True,
        unexpected_items=0,
    )
    if not isinstance(raw, dict) or set(raw) != {"results"} or not isinstance(raw["results"], list):
        return invalid_all

    requested_set = set(requested)
    allowed_set = set(allowed)
    parsed: dict[int, list[_Item]] = {}
    unexpected = 0
    for entry in raw["results"]:
        try:
            item = _Item.model_validate(entry)
        except ValidationError:
            unexpected += 1
            continue
        if item.i not in requested_set:
            unexpected += 1
            continue
        parsed.setdefault(item.i, []).append(item)

    outcomes: dict[int, ItemOutcome] = {}
    for index in requested:
        entries = parsed.get(index, [])
        if len(entries) != 1:  # missing, or answered twice (ambiguous): never pick one
            outcomes[index] = ItemOutcome(index, Verdict.INVALID)
            continue
        item = entries[0]
        reason = _clean_reason(item.reason)
        if item.needs_review or item.category == NEEDS_REVIEW:
            outcomes[index] = ItemOutcome(index, Verdict.ABSTAINED, None, reason)
        elif item.category in allowed_set:
            outcomes[index] = ItemOutcome(index, Verdict.ACCEPTED, item.category, reason)
        else:
            outcomes[index] = ItemOutcome(index, Verdict.INVALID)
    return ClassificationOutcome(
        items=outcomes, structural_error=False, unexpected_items=unexpected
    )
