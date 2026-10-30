"""Import staging models: what an importer extracts and what the user decides.

Importers produce a ``RawTable`` of strings and never interpret money or dates. The user (or a
saved profile) supplies a ``ColumnMapping`` and ``ImportChoices``; every choice that could be
guessed wrongly (date format, separators, sign convention) is required, not defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bank_analyzer.domain.dates import DATE_FORMATS
from bank_analyzer.domain.models import CURRENCY_PATTERN, CanonicalTransaction, FileKind
from bank_analyzer.domain.validation import ValidationIssue


class SignConvention(StrEnum):
    """How the amount column(s) map to direction. Always chosen explicitly."""

    NEGATIVE_IS_OUTFLOW = "negative_is_outflow"  # typical bank export: debits negative
    POSITIVE_IS_OUTFLOW = "positive_is_outflow"  # typical card export: purchases positive
    DEBIT_CREDIT_COLUMNS = "debit_credit_columns"  # separate debit and credit columns
    INDICATOR_DR_CR = "indicator_dr_cr"  # unsigned amount plus a DR/CR column


@dataclass(frozen=True, slots=True)
class RawRow:
    """One extracted row of text cells. ``row_index`` is the importer's source row number."""

    row_index: int
    cells: tuple[str, ...]
    location: str | None = None  # sheet name or "page N"
    confidence: float | None = None  # OCR only: lowest word confidence in the row, 0-100


@dataclass(frozen=True, slots=True)
class RawTable:
    """Extractor output. Contains data rows only; the header row is kept separately."""

    file_sha256: str
    file_kind: FileKind
    headers: tuple[str, ...]
    rows: tuple[RawRow, ...]
    warnings: tuple[str, ...] = field(default=())
    # Text around the table (statement header and footer), where a PDF has it. Used only to help
    # a person or a local model understand the layout; never parsed as transactions.
    context_text: str = ""


class ColumnMapping(BaseModel):
    """Zero-based column indexes into ``RawRow.cells``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date_col: int = Field(ge=0)
    description_col: int = Field(ge=0)
    amount_col: int | None = Field(default=None, ge=0)
    debit_col: int | None = Field(default=None, ge=0)
    credit_col: int | None = Field(default=None, ge=0)
    indicator_col: int | None = Field(default=None, ge=0)
    currency_col: int | None = Field(default=None, ge=0)
    balance_col: int | None = Field(default=None, ge=0)
    booking_date_col: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        has_amount = self.amount_col is not None
        has_split = self.debit_col is not None or self.credit_col is not None
        if has_amount == has_split:
            raise ValueError("map either one amount column or debit/credit columns, not both")
        return self


class ImportChoices(BaseModel):
    """Explicit user decisions for one import."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    default_currency: str = Field(pattern=CURRENCY_PATTERN)
    date_format: str
    decimal_sep: str
    thousands_sep: str | None
    sign_convention: SignConvention

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.date_format not in DATE_FORMATS:
            raise ValueError("unsupported date format")
        return self


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Outcome of normalizing a RawTable. Nothing here is persisted yet."""

    transactions: tuple[CanonicalTransaction, ...]
    issues: tuple[ValidationIssue, ...]
    rows_read: int
    rows_skipped: int  # blank rows, footers and merged continuation lines
    rows_rejected: int  # rows with errors; never imported
    # Source row numbers whose OCR confidence was below the threshold: the user must confirm
    # the amounts of these rows before the import may be committed.
    low_confidence_rows: tuple[int, ...] = ()

    @property
    def has_errors(self) -> bool:
        return self.rows_rejected > 0

    def issue_counts(self) -> dict[str, int]:
        """Counts per issue code, suitable for a batch warning summary."""
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.code] = counts.get(issue.code, 0) + 1
        return dict(sorted(counts.items()))
