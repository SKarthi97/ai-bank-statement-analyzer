"""Explicit date parsing and format detection.

Statement dates are civil dates (no time zone). A format is always chosen explicitly; detection
only proposes candidates and reports when more than one reading is valid.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from bank_analyzer.domain.errors import DateParseError

DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%m/%d/%y",
    "%d.%m.%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def has_two_digit_year(fmt: str) -> bool:
    """True if ``fmt`` uses a two-digit year, which the caller must flag for review."""
    return "%y" in fmt


def parse_date(text: str, fmt: str) -> date:
    """Parse ``text`` with one of the supported formats; raise DateParseError on failure."""
    if fmt not in DATE_FORMATS:
        raise ValueError(f"unsupported date format: {fmt}")
    try:
        return datetime.strptime(_clean(text), fmt).date()
    except ValueError as exc:
        raise DateParseError("date does not match the selected format") from exc


@dataclass(frozen=True, slots=True)
class DateFormatGuess:
    """Formats under which every sample parses; ``ambiguous`` means the user must choose."""

    candidates: tuple[str, ...]
    ambiguous: bool
    two_digit_year: bool


def detect_date_format(samples: Iterable[str]) -> DateFormatGuess:
    """Propose date formats consistent with all non-blank samples.

    Formats that produce identical dates for every sample (for example ``%b`` vs ``%B`` on
    "05 May 2024") are collapsed. ``01/02/2024`` alone is ambiguous between day-first and
    month-first; a single sample such as ``25/01/2024`` resolves it.
    """
    values = [s for s in samples if s.strip()]
    seen: dict[tuple[date, ...], str] = {}
    for fmt in DATE_FORMATS:
        try:
            result = tuple(parse_date(v, fmt) for v in values)
        except DateParseError:
            continue
        seen.setdefault(result, fmt)
    candidates = tuple(seen.values())
    return DateFormatGuess(
        candidates=candidates,
        ambiguous=len(candidates) > 1,
        two_digit_year=any(has_two_digit_year(c) for c in candidates),
    )
