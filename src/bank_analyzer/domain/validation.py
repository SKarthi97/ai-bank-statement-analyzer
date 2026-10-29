"""Validation issues and text sanitising.

Issues never contain statement content, only codes, counts and positions, so they are safe to
display and log.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from bank_analyzer.domain.models import MAX_DESCRIPTION_LENGTH, CanonicalTransaction


class Severity(StrEnum):
    ERROR = "error"  # row cannot be imported
    WARNING = "warning"  # row imported, user should look
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A problem found while importing; ``row_index`` is the importer's source row number."""

    severity: Severity
    code: str
    message: str
    row_index: int | None = None


# Issue codes (stable identifiers for UI, tests and batch warning summaries).
E_MISSING_DATE = "E_MISSING_DATE"
E_BAD_DATE = "E_BAD_DATE"
E_NO_AMOUNT = "E_NO_AMOUNT"
E_BAD_AMOUNT = "E_BAD_AMOUNT"
E_BOTH_DEBIT_CREDIT = "E_BOTH_DEBIT_CREDIT"
E_BAD_INDICATOR = "E_BAD_INDICATOR"
E_BAD_CURRENCY = "E_BAD_CURRENCY"
W_ZERO_AMOUNT = "W_ZERO_AMOUNT"
W_EMPTY_DESCRIPTION = "W_EMPTY_DESCRIPTION"
W_DESCRIPTION_TRUNCATED = "W_DESCRIPTION_TRUNCATED"
W_BAD_BALANCE = "W_BAD_BALANCE"
W_BAD_BOOKING_DATE = "W_BAD_BOOKING_DATE"
W_SIGN_IN_DEBIT_CREDIT = "W_SIGN_IN_DEBIT_CREDIT"
W_TWO_DIGIT_YEAR = "W_TWO_DIGIT_YEAR"
W_FUTURE_DATE = "W_FUTURE_DATE"
W_OLD_DATE = "W_OLD_DATE"
I_FOOTER_SKIPPED = "I_FOOTER_SKIPPED"
I_CONTINUATION_MERGED = "I_CONTINUATION_MERGED"

_EARLIEST_PLAUSIBLE = date(1990, 1, 1)


def sanitize_text(text: str) -> tuple[str, bool]:
    """Remove control characters, collapse whitespace and cap the length.

    Returns ``(clean_text, was_truncated)``. The result is plain text; callers must still
    treat it as untrusted when rendering, exporting or sending to a model.
    """
    cleaned = "".join(
        " " if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} else ch for ch in text
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        return cleaned[:MAX_DESCRIPTION_LENGTH].rstrip(), True
    return cleaned, False


def validate_transaction(
    txn: CanonicalTransaction, *, row_index: int | None, today: date
) -> list[ValidationIssue]:
    """Row-level sanity warnings for a constructed transaction."""
    issues: list[ValidationIssue] = []
    if txn.amount_minor == 0:
        issues.append(
            ValidationIssue(Severity.WARNING, W_ZERO_AMOUNT, "Amount is zero.", row_index)
        )
    if not txn.original_description:
        issues.append(
            ValidationIssue(
                Severity.WARNING, W_EMPTY_DESCRIPTION, "Description is empty.", row_index
            )
        )
    if txn.txn_date > today + timedelta(days=1):
        issues.append(
            ValidationIssue(Severity.WARNING, W_FUTURE_DATE, "Date is in the future.", row_index)
        )
    if txn.txn_date < _EARLIEST_PLAUSIBLE:
        issues.append(
            ValidationIssue(Severity.WARNING, W_OLD_DATE, "Date is before 1990.", row_index)
        )
    return issues
