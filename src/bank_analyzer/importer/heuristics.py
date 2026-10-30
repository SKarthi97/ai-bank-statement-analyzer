"""Work out a statement's layout by rules alone, with no model.

Most statements are readable this way: the column names are standard (Date, Description or
Particulars, Debit/Payments, Credit/Receipts, Balance), the number format follows from the amounts,
and the opening and closing rows and footer totals have recognisable wording. A layout guessed here
is exactly as untrusted as one proposed by a model: it must pass ``check_layout`` before use.

Anything that would need a guess returns None (an ambiguous date or number format, an unsigned
single amount column), so the caller falls back to a model or to the person.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import ValidationError

from bank_analyzer.domain.dates import DATE_FORMATS, detect_date_format
from bank_analyzer.domain.imports import RawTable, SignConvention
from bank_analyzer.domain.money import detect_number_format
from bank_analyzer.importer.column_mapper import suggest_mapping
from bank_analyzer.importer.layout import YEARLESS_DATE_FORMATS, StatementLayout

_SAMPLE_ROWS = 60
_CURRENCY = re.compile(r"\bCURRENCY\s*[:\-]?\s*([A-Z]{3})\b")
_OPENING = re.compile(
    r"^(balance\s*(b\s*/\s*f|brought\s*forward)|opening\s*balance|previous\s*balance)$", re.I
)
_CLOSING = re.compile(r"^(balance\s*(c\s*/\s*[fd]|carried\s*forward)|closing\s*balance)$", re.I)
_NUMBER = re.compile(r"\d(?:[\d,.']*\d)?")
_IN_WORDS = re.compile(r"deposit|credit|receipt|money in|paid in", re.I)
_OUT_WORDS = re.compile(r"withdraw|debit|payment|money out|paid out", re.I)


def _column(raw: RawTable, index: int) -> list[str]:
    out: list[str] = []
    for row in raw.rows[:_SAMPLE_ROWS]:
        if index < len(row.cells) and row.cells[index].strip():
            out.append(row.cells[index].strip())
    return out


def _date_format(samples: list[str]) -> str | None:
    """The one date format every sample fits, including formats with no year; else None."""
    if not samples:
        return None
    guess = detect_date_format(samples)
    if len(guess.candidates) == 1 and not guess.two_digit_year:
        return guess.candidates[0]
    if guess.candidates:
        return None  # ambiguous (day-first or month-first) or two-digit year: a person decides
    fits: list[str] = []
    for fmt in YEARLESS_DATE_FORMATS:
        try:
            for sample in samples:
                datetime.strptime(f"{sample} 2000", f"{fmt} %Y")
        except ValueError:
            continue
        fits.append(fmt)
    return fits[0] if len(fits) == 1 else None


def _stated_totals(text: str) -> tuple[str | None, str | None]:
    total_in = total_out = None
    for line in text.splitlines():
        if "total" not in line.lower():
            continue
        numbers = _NUMBER.findall(line)
        if not numbers:
            continue
        if _IN_WORDS.search(line):
            total_in = total_in or numbers[-1]
        elif _OUT_WORDS.search(line):
            total_out = total_out or numbers[-1]
    return total_in, total_out


def guess_layout(raw: RawTable, *, default_currency: str) -> StatementLayout | None:
    """A layout from rules alone, or None if it cannot be had without guessing."""
    found = suggest_mapping(raw.headers).found
    if "date" not in found or "description" not in found:
        return None
    split = "debit" in found or "credit" in found
    if not split and "amount" not in found:
        return None

    currency_match = _CURRENCY.search(raw.context_text)
    currency = currency_match.group(1) if currency_match else default_currency

    date_format = _date_format(_column(raw, found["date"]))
    if date_format is None or date_format not in (*DATE_FORMATS, *YEARLESS_DATE_FORMATS):
        return None

    money_cells: list[str] = []
    for role in ("amount", "debit", "credit", "balance"):
        if role in found:
            money_cells += _column(raw, found[role])
    number = detect_number_format(money_cells, currency=currency)
    if len(number.candidates) != 1:
        return None
    separators = number.candidates[0]

    if split:
        sign = SignConvention.DEBIT_CREDIT_COLUMNS
    elif "indicator" in found:
        sign = SignConvention.INDICATOR_DR_CR
    else:
        amounts = _column(raw, found["amount"])
        if not any(a.startswith(("-", "(")) or a.endswith("-") for a in amounts):
            return None  # an unsigned single column: which way money moves is not knowable
        sign = SignConvention.NEGATIVE_IS_OUTFLOW

    headers = raw.headers
    opening = closing = None
    for row in raw.rows:
        label = " ".join(row.cells[found["description"]].split()) if row.cells else ""
        if opening is None and _OPENING.match(label):
            opening = label
        elif closing is None and _CLOSING.match(label):
            closing = label
    total_in, total_out = _stated_totals(raw.context_text)

    def name(role: str) -> str | None:
        return headers[found[role]] if role in found else None

    try:
        return StatementLayout(
            currency=currency,
            date_format=date_format,
            decimal_sep=separators.decimal_sep,
            thousands_sep=separators.thousands_sep,
            sign_convention=sign,
            date_header=headers[found["date"]],
            description_header=headers[found["description"]],
            amount_header=None if split else name("amount"),
            debit_header=name("debit") if split else None,
            credit_header=name("credit") if split else None,
            indicator_header=name("indicator") if sign == SignConvention.INDICATOR_DR_CR else None,
            balance_header=name("balance"),
            opening_row_label=opening,
            closing_row_label=closing,
            stated_total_in=total_in,
            stated_total_out=total_out,
        )
    except ValidationError:
        return None


def repair_model_layout(
    raw: RawTable, layout: StatementLayout
) -> tuple[StatementLayout, list[str]]:
    """Correct what rules can establish better than a model: number separators, balance column.

    A small model often gets "1,643.10" backwards or forgets the balance column. Both are facts
    about the document, not judgement, so the rules win. Returns the layout and a note per change.
    """
    notes: list[str] = []
    found = suggest_mapping(raw.headers).found
    changes: dict[str, object] = {}

    roles = ("amount_header", "debit_header", "credit_header", "balance_header")
    headers = [h for h in (getattr(layout, r) for r in roles) if h]
    cells: list[str] = []
    for header in headers:
        index = next((i for i, h in enumerate(raw.headers) if h == header), None)
        if index is not None:
            cells += _column(raw, index)
    guess = detect_number_format(cells, currency=layout.currency)
    if len(guess.candidates) == 1:
        sep = guess.candidates[0]
        if (sep.decimal_sep, sep.thousands_sep) != (layout.decimal_sep, layout.thousands_sep):
            changes.update(decimal_sep=sep.decimal_sep, thousands_sep=sep.thousands_sep)
            notes.append("The number separators were taken from the amounts, not from the AI.")
    if layout.balance_header is None and "balance" in found:
        changes["balance_header"] = raw.headers[found["balance"]]
        notes.append("The balance column was identified from its heading.")
    if not changes:
        return layout, notes
    try:
        return layout.model_copy(update=changes), notes
    except ValidationError:  # pragma: no cover - the changes are individually valid
        return layout, []


def dates_have_no_year(samples: list[str]) -> bool:
    """True if every sample is a date written without a year ("28/10", "28 Oct")."""
    values = [s.strip() for s in samples if s.strip()]
    return (
        bool(values)
        and _date_format(values) is not None
        and not detect_date_format(values).candidates
    )
