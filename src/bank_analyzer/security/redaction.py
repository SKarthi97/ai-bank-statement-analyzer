"""Removing financial and personal data from text that may be logged.

The logging policy is "do not log data": log events, counts and ids, never descriptions or amounts.
Redaction is a safety net for the case where something slips through, not a licence to log
statement content. It is pattern-based, so it cannot recognise a free-text description; it removes
what is recognisable (money amounts, long digit runs such as account and card numbers, IBANs,
e-mail addresses, and the query part of URLs).
"""

from __future__ import annotations

import re
import traceback
from types import TracebackType

REDACTED = "[redacted]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail
    re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),  # IBAN-like
    re.compile(
        r"(?<![\w-])\d(?:[ -]?\d){5,}(?![\w-])"
    ),  # six or more digits, even grouped (cards, accounts); ids with hyphens are left alone
    re.compile(r"[-+(]?\d{1,3}(?:[,. ]\d{3})*[.,]\d{2}\)?(?!\d)"),  # 1,234.50  -85,40  (12.00)
    re.compile(r"[-+]?\d+[.,]\d{1,2}\b"),  # 85.4  3,5
    re.compile(r"\?[^\s\"']*"),  # URL query strings
)


def redact_text(text: str) -> str:
    """Replace recognisable financial and personal data in ``text`` with ``[redacted]``."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def describe_exception(exc: BaseException | None) -> str:
    """The exception's class name only. Messages can contain data, so they are never included."""
    return type(exc).__name__ if exc is not None else "unknown error"


def format_exc_info(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> str:
    """A traceback summary with file, line and function names but no exception message or locals."""
    exc_type, _, tb = exc_info
    frames = [
        f"{frame.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(tb)
    ]
    return f"{exc_type.__name__} (message omitted)\n  " + "\n  ".join(frames)
