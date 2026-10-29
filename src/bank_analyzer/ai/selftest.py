"""A quick check that a model returns usable structured output (used by ``scripts/check_model.py``
and before switching models)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bank_analyzer.ai.local_llm import LLMError, LocalLLM
from bank_analyzer.ai.prompts import (
    ClassificationItem,
    build_classification_messages,
    classification_schema,
)
from bank_analyzer.ai.response_validator import Verdict, validate_classification

# Invented descriptions; no real data is ever used for a self-test.
SELFTEST_ITEMS: tuple[ClassificationItem, ...] = (
    ClassificationItem(0, "NETFLIX COM", "out"),
    ClassificationItem(1, "CITY POWER AND LIGHT", "out"),
    ClassificationItem(2, "SALARY ACME CORP", "in"),
    ClassificationItem(3, "WEEKLY GROCERY MART", "out"),
    ClassificationItem(4, "XQZ 12 UNKNOWN LTD", "out"),
)


@dataclass(frozen=True, slots=True)
class SelfTestResult:
    reachable: bool
    usable: bool  # every item came back as a valid, schema-conforming answer
    answered: int  # accepted or abstained
    total: int
    detail: str


def run_selftest(llm: LocalLLM, categories: Sequence[str]) -> SelfTestResult:
    """Send five invented descriptions and check the reply validates. Never raises."""
    total = len(SELFTEST_ITEMS)
    system, user = build_classification_messages(SELFTEST_ITEMS, categories)
    try:
        raw = llm.chat_json(system, user, classification_schema(categories))
    except LLMError as exc:
        return SelfTestResult(False, False, 0, total, str(exc))
    outcome = validate_classification(
        raw, requested=[i.index for i in SELFTEST_ITEMS], allowed=categories
    )
    answered = outcome.count(Verdict.ACCEPTED) + outcome.count(Verdict.ABSTAINED)
    usable = not outcome.structural_error and answered == total
    detail = (
        "The model returned valid structured output."
        if usable
        else ("The model's reply did not match the required format.")
    )
    return SelfTestResult(True, usable, answered, total, detail)
