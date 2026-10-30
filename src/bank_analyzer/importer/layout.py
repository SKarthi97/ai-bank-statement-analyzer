"""A statement's layout, and the deterministic code that applies and checks it.

A ``StatementLayout`` says how to *read* a statement: which column is which, the date format and the
year of dates that have none, the number separators, the sign convention, which rows are not
transactions (``BALANCE B/F``), and the opening, closing and footer totals the document states. It
can come from a person, a saved profile or a local model (``ai.statement_reader``); where it came
from does not matter, because it is validated here and then *checked against the document's own
arithmetic* before anything is imported.

The layout never carries amounts. Every amount is parsed from the document's text by the existing
strict parsers, so a model that misreads a layout is caught by ``check_layout`` (running balance,
closing balance, stated totals) instead of silently producing wrong money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bank_analyzer.domain.dates import DATE_FORMATS
from bank_analyzer.domain.errors import AmountParseError, MappingError
from bank_analyzer.domain.imports import (
    ColumnMapping,
    ImportChoices,
    RawRow,
    RawTable,
    SignConvention,
)
from bank_analyzer.domain.models import CURRENCY_PATTERN
from bank_analyzer.domain.money import parse_amount

# Dates without a year. They are resolved to full dates here (the year comes from the statement's
# own end date or year) and then read with "%Y-%m-%d", so the rest of the pipeline never sees them.
YEARLESS_DATE_FORMATS: tuple[str, ...] = ("%d/%m", "%d-%m", "%d.%m", "%d %b", "%d %B")
RESOLVED_DATE_FORMAT = "%Y-%m-%d"
_LABEL_LIMIT = 60


class StatementLayout(BaseModel):
    """How to read one statement. Column roles are header *names*, resolved to indexes later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bank_name: str | None = Field(default=None, max_length=80)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    date_format: str
    statement_end: date | None = None  # the date the statement runs to (year source)
    year: int | None = Field(default=None, ge=1990, le=2100)  # used when there is no end date
    decimal_sep: str
    thousands_sep: str | None = None
    sign_convention: SignConvention
    date_header: str
    description_header: str
    amount_header: str | None = None
    debit_header: str | None = None
    credit_header: str | None = None
    indicator_header: str | None = None
    balance_header: str | None = None
    ignore_row_labels: tuple[str, ...] = Field(default=(), max_length=20)
    opening_row_label: str | None = Field(default=None, max_length=_LABEL_LIMIT)
    closing_row_label: str | None = Field(default=None, max_length=_LABEL_LIMIT)
    stated_total_in: str | None = Field(default=None, max_length=40)
    stated_total_out: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _check(self) -> Self:
        yearless = self.date_format in YEARLESS_DATE_FORMATS
        if self.date_format not in DATE_FORMATS and not yearless:
            raise ValueError("date_format is not a supported format")
        split = self.debit_header is not None or self.credit_header is not None
        if self.sign_convention == SignConvention.DEBIT_CREDIT_COLUMNS:
            if not split or self.amount_header is not None:
                raise ValueError(
                    "debit_credit_columns needs debit_header and/or credit_header and no amount_header"
                )
        elif split or self.amount_header is None:
            raise ValueError(
                "this sign_convention needs amount_header and no debit_header or credit_header"
            )
        if self.sign_convention == SignConvention.INDICATOR_DR_CR and not self.indicator_header:
            raise ValueError("indicator_dr_cr needs indicator_header")
        if any(not label.strip() or len(label) > _LABEL_LIMIT for label in self.ignore_row_labels):
            raise ValueError("ignore_row_labels must be short, non-empty labels")
        return self


@dataclass(frozen=True, slots=True)
class LayoutApplication:
    """A layout applied to a table: ready for ``normalize``, plus what the layout stated."""

    table: RawTable
    mapping: ColumnMapping
    choices: ImportChoices
    opening_minor: int | None
    closing_minor: int | None
    notes: tuple[str, ...]


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _header_index(headers: tuple[str, ...], name: str | None, role: str) -> int | None:
    if name is None:
        return None
    wanted = _fold(name)
    found = [i for i, h in enumerate(headers) if _fold(h) == wanted]
    if not found:
        raise MappingError(f"The {role} column '{name}' is not one of the statement's columns.")
    if len(found) > 1:
        raise MappingError(f"The {role} column '{name}' matches more than one column.")
    return found[0]


def _cell(row: RawRow, index: int | None) -> str:
    if index is None or index >= len(row.cells):
        return ""
    return row.cells[index].strip()


_FULL_DATE_FORMS: dict[str, tuple[re.Pattern[str], tuple[str, ...]]] = {
    "%d/%m": (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), ("%d/%m/%Y",)),
    "%d-%m": (re.compile(r"\b\d{1,2}-\d{1,2}-\d{4}\b"), ("%d-%m-%Y",)),
    "%d.%m": (re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b"), ("%d.%m.%Y",)),
    "%d %b": (re.compile(r"\b\d{1,2} [A-Za-z]{3,9},? \d{4}\b"), ("%d %b %Y", "%d %B %Y")),
}
_FULL_DATE_FORMS["%d %B"] = _FULL_DATE_FORMS["%d %b"]


def infer_statement_end(text: str, date_format: str) -> date | None:
    """The latest full date written in the statement's own text, in the table's date style.

    Statements that print dates without a year usually print the statement date or period in the
    heading. This is deterministic and is always shown to the user, who must check it.
    """
    form = _FULL_DATE_FORMS.get(date_format)
    if form is None:
        return None
    pattern, formats = form
    found: list[date] = []
    for match in pattern.finditer(text):
        for fmt in formats:
            try:
                found.append(datetime.strptime(match.group(0).replace(",", ""), fmt).date())
                break
            except ValueError:
                continue
    return max(found) if found else None


def _resolve_year(
    text: str,
    date_format: str,
    end: date | None,
    year_hint: int | None,
    previous_month: int | None,
) -> tuple[date | None, int | None]:
    """Complete a yearless date. Returns (date or None if unreadable, month for rollover)."""
    try:
        # 2000 is a leap year, so 29 Feb parses here; it is rejected below for other years.
        parsed = datetime.strptime(f"{text.strip()} 2000", f"{date_format} %Y")
    except ValueError:
        return None, previous_month
    month, day = parsed.month, parsed.day
    if end is not None:
        year = end.year if (month, day) <= (end.month, end.day) else end.year - 1
    else:
        assert year_hint is not None
        year = year_hint
        if previous_month is not None and month < previous_month:
            year += 1  # dates ran past December: the statement crosses into the next year
    try:
        return date(year, month, day), month
    except ValueError:
        return None, month


def apply_layout(raw: RawTable, layout: StatementLayout, *, account_id: str) -> LayoutApplication:
    """Resolve the layout against the table. Raises MappingError if it does not fit."""
    headers = raw.headers
    date_col = _header_index(headers, layout.date_header, "date")
    description_col = _header_index(headers, layout.description_header, "description")
    if date_col is None or description_col is None:  # pragma: no cover - both are required
        raise MappingError("The date and description columns are required.")
    amount_col = _header_index(headers, layout.amount_header, "amount")
    debit_col = _header_index(headers, layout.debit_header, "debit")
    credit_col = _header_index(headers, layout.credit_header, "credit")
    indicator_col = _header_index(headers, layout.indicator_header, "indicator")
    balance_col = _header_index(headers, layout.balance_header, "balance")
    try:
        mapping = ColumnMapping(
            date_col=date_col,
            description_col=description_col,
            amount_col=amount_col,
            debit_col=debit_col,
            credit_col=credit_col,
            indicator_col=indicator_col,
            balance_col=balance_col,
        )
    except ValueError as exc:
        raise MappingError("The layout's amount columns are inconsistent.") from exc

    ignore = {_fold(label) for label in layout.ignore_row_labels}
    opening_label = _fold(layout.opening_row_label) if layout.opening_row_label else None
    closing_label = _fold(layout.closing_row_label) if layout.closing_row_label else None
    yearless = layout.date_format in YEARLESS_DATE_FORMATS
    end, year_hint, found_in_text = layout.statement_end, layout.year, False
    if yearless and end is None and year_hint is None:
        end = infer_statement_end(raw.context_text, layout.date_format)
        found_in_text = end is not None
        if end is None:
            raise MappingError(
                "The dates have no year and no statement date was found in the document text. "
                "Use the manual steps and enter the year."
            )

    opening: int | None = None
    closing: int | None = None
    kept: list[RawRow] = []
    dropped = 0
    unreadable_dates = 0
    previous_month: int | None = None
    for row in raw.rows:
        label = _fold(_cell(row, description_col))
        if label in ignore or label in (opening_label, closing_label):
            dropped += 1
            balance_text = _cell(row, balance_col)
            if balance_text and label in (opening_label, closing_label):
                try:
                    value = parse_amount(
                        balance_text,
                        currency=layout.currency,
                        decimal_sep=layout.decimal_sep,
                        thousands_sep=layout.thousands_sep,
                    ).minor
                except AmountParseError:
                    value = None
                if label == opening_label and opening is None:
                    opening = value
                if label == closing_label:
                    closing = value
            continue
        if yearless:
            resolved, previous_month = _resolve_year(
                _cell(row, date_col), layout.date_format, end, year_hint, previous_month
            )
            if resolved is None:
                unreadable_dates += 1  # left as written; normalization will reject the row
            else:
                cells = list(row.cells)
                cells[date_col] = resolved.isoformat()
                row = RawRow(row.row_index, tuple(cells), row.location, row.confidence)
        kept.append(row)

    notes: list[str] = []
    if yearless:
        if end is not None:
            where = " (found in the document text)" if found_in_text else ""
            source = f"the statement end date {end.isoformat()}{where}"
        else:
            source = f"the year {year_hint}"
        notes.append(f"Dates have no year; the year was taken from {source}. Check it.")
    if dropped:
        notes.append(
            f"{dropped} non-transaction row(s) (opening/closing balance lines) were set aside."
        )
    if unreadable_dates:
        notes.append(f"{unreadable_dates} row(s) had a date that could not be completed.")

    table = RawTable(
        file_sha256=raw.file_sha256,
        file_kind=raw.file_kind,
        headers=raw.headers,
        rows=tuple(kept),
        warnings=(*raw.warnings, *notes),
    )
    choices = ImportChoices(
        account_id=account_id,
        default_currency=layout.currency,
        date_format=RESOLVED_DATE_FORMAT if yearless else layout.date_format,
        decimal_sep=layout.decimal_sep,
        thousands_sep=layout.thousands_sep,
        sign_convention=layout.sign_convention,
    )
    return LayoutApplication(table, mapping, choices, opening, closing, tuple(notes))


# -- checking the interpretation against the document's own arithmetic ----------------------


@dataclass(frozen=True, slots=True)
class CheckItem:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class LayoutCheck:
    """Whether the document's own numbers agree with how the layout read it."""

    items: tuple[CheckItem, ...]
    rows_in: int
    rows_out: int
    total_in_minor: int
    total_out_minor: int  # a positive number: money that left

    @property
    def consistent(self) -> bool:
        """True only if at least one check ran and every check that ran passed."""
        return bool(self.items) and all(item.ok for item in self.items)


def _amount(text: str, layout: StatementLayout) -> int | None:
    if not text.strip():
        return None
    try:
        return parse_amount(
            text,
            currency=layout.currency,
            decimal_sep=layout.decimal_sep,
            thousands_sep=layout.thousands_sep,
        ).minor
    except AmountParseError:
        return None


def check_layout(application: LayoutApplication, layout: StatementLayout) -> LayoutCheck:
    """Test the layout against the statement: running balance, closing balance, stated totals.

    Money here is signed so that inflows are positive. Only conventions that can be read without
    a DR/CR indicator are checked; for others the check says so instead of guessing.
    """
    mapping = application.mapping
    items: list[CheckItem] = []
    effects: list[tuple[int, int | None]] = []  # (signed effect, balance after)
    total_in = total_out = rows_in = rows_out = 0
    unreadable = 0
    sign = layout.sign_convention
    for row in application.table.rows:
        if sign == SignConvention.DEBIT_CREDIT_COLUMNS:
            out_value = _amount(_cell(row, mapping.debit_col), layout)
            in_value = _amount(_cell(row, mapping.credit_col), layout)
            if out_value is None and in_value is None:
                unreadable += 1
                continue
            effect = (in_value or 0) - abs(out_value or 0)
        elif sign in (SignConvention.NEGATIVE_IS_OUTFLOW, SignConvention.POSITIVE_IS_OUTFLOW):
            value = _amount(_cell(row, mapping.amount_col), layout)
            if value is None:
                unreadable += 1
                continue
            effect = value if sign == SignConvention.NEGATIVE_IS_OUTFLOW else -value
        else:
            return LayoutCheck(
                (CheckItem("checks", False, "DR/CR indicator layouts are not checked yet."),),
                0,
                0,
                0,
                0,
            )
        if effect > 0:
            total_in += effect
            rows_in += 1
        else:
            total_out += -effect
            rows_out += 1
        effects.append((effect, _amount(_cell(row, mapping.balance_col), layout)))

    if unreadable:
        items.append(CheckItem("rows", False, f"{unreadable} row(s) had no readable amount."))

    balances = [(i, e, b) for i, (e, b) in enumerate(effects) if b is not None]
    if balances:
        previous = application.opening_minor
        bad: list[int] = []
        for index, effect, balance in balances:
            if previous is not None and previous + effect != balance:
                bad.append(index + 1)
            previous = balance
        if application.opening_minor is None:
            items.append(
                CheckItem("running balance", not bad, _chain_detail(bad, "no opening balance"))
            )
        else:
            items.append(CheckItem("running balance", not bad, _chain_detail(bad, "from opening")))
        if application.closing_minor is not None:
            ok = balances[-1][2] == application.closing_minor
            items.append(
                CheckItem(
                    "closing balance",
                    ok,
                    "last balance equals the stated closing balance"
                    if ok
                    else "last balance differs from the stated closing balance",
                )
            )
    for name, stated, actual in (
        ("total deposits", layout.stated_total_in, total_in),
        ("total withdrawals", layout.stated_total_out, total_out),
    ):
        if stated is None:
            continue
        wanted = _amount(stated, layout)
        if wanted is None:
            items.append(CheckItem(name, False, "the stated total could not be read."))
        else:
            ok = abs(wanted) == actual
            items.append(
                CheckItem(
                    name, ok, "matches the rows" if ok else "does not match the sum of the rows"
                )
            )
    return LayoutCheck(tuple(items), rows_in, rows_out, total_in, total_out)


def _chain_detail(bad: list[int], basis: str) -> str:
    if not bad:
        return f"every balance follows from the one before ({basis})"
    shown = ", ".join(str(n) for n in bad[:5])
    more = "" if len(bad) <= 5 else f" and {len(bad) - 5} more"
    return f"balance does not follow at row(s) {shown}{more}"
