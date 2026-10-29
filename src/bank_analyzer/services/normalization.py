"""Turn extracted rows into canonical transactions.

Nothing is guessed silently: the date format, separators and sign convention come from the
user's ``ImportChoices``. Rows that cannot be interpreted are rejected with a coded issue, never
repaired. ``detect_formats`` only proposes choices and reports ambiguity.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from bank_analyzer.domain.dates import (
    DateFormatGuess,
    detect_date_format,
    has_two_digit_year,
    parse_date,
)
from bank_analyzer.domain.errors import (
    AmountParseError,
    DateParseError,
    MappingError,
)
from bank_analyzer.domain.imports import (
    ColumnMapping,
    ImportChoices,
    NormalizationResult,
    RawRow,
    RawTable,
    SignConvention,
)
from bank_analyzer.domain.models import CanonicalTransaction, SourceRef, new_id, utc_now_iso
from bank_analyzer.domain.money import (
    Money,
    NumberFormatGuess,
    detect_number_format,
    parse_amount,
    validate_currency,
)
from bank_analyzer.domain.validation import (
    E_BAD_AMOUNT,
    E_BAD_CURRENCY,
    E_BAD_DATE,
    E_BAD_INDICATOR,
    E_BOTH_DEBIT_CREDIT,
    E_MISSING_DATE,
    E_NO_AMOUNT,
    I_CONTINUATION_MERGED,
    I_FOOTER_SKIPPED,
    W_BAD_BALANCE,
    W_BAD_BOOKING_DATE,
    W_DESCRIPTION_TRUNCATED,
    W_LOW_OCR_CONFIDENCE,
    W_SIGN_IN_DEBIT_CREDIT,
    W_TWO_DIGIT_YEAR,
    Severity,
    ValidationIssue,
    sanitize_text,
    validate_transaction,
)

# --- description normalization -------------------------------------------------------------

_CARD_MASK = re.compile(r"\b(?:X{2,}|\*{2,})\s?\d{2,4}\b|\*\d{2,4}\b")
_CARD_NUMBER = re.compile(r"\bCARD\s*(?:NO\.?)?\s*\d{2,4}\b")
_REF_TOKEN = re.compile(r"\b(?:REF|REFERENCE|TXN|TRN)\s*[:#]?\s*\S+")
_HASH_NUMBER = re.compile(r"#\s*\d+")
_DATE_TOKEN = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{1,2}[-.]\d{1,2}[-.]\d{2,4}\b")
_LONG_DIGITS = re.compile(r"\b\d{5,}\b")
_NON_WORD = re.compile(r"[^A-Z0-9&' ]+")
_FOOTER = re.compile(
    r"^(total|totals|closing balance|opening balance|balance (brought|carried) forward|"
    r"statement total)\b",
    re.IGNORECASE,
)
_MERCHANT_STOPWORDS = frozenset(
    {
        "POS",
        "PURCHASE",
        "DEBIT",
        "CARD",
        "PAYMENT",
        "PMT",
        "TO",
        "FROM",
        "THE",
        "VISA",
        "MASTERCARD",
        "ONLINE",
        "WWW",
        "COM",
        "AND",
        "OF",
    }
)
_DEBIT_INDICATORS = frozenset({"DR", "D", "DB", "DEBIT"})
_CREDIT_INDICATORS = frozenset({"CR", "C", "CREDIT"})


def normalize_description(text: str) -> str:
    """Upper-case the description and strip volatile tokens (card digits, references, dates).

    The result is only used for matching and grouping; the original text is always preserved.
    """
    s = unicodedata.normalize("NFKC", text).upper()
    for pattern in (_CARD_MASK, _CARD_NUMBER, _REF_TOKEN, _HASH_NUMBER, _DATE_TOKEN, _LONG_DIGITS):
        s = pattern.sub(" ", s)
    s = _NON_WORD.sub(" ", s)
    return " ".join(s.split())


def merchant_key(normalized: str) -> str | None:
    """A stable grouping key: the first three meaningful tokens, or None if there are none."""
    tokens = [t for t in normalized.split() if t not in _MERCHANT_STOPWORDS and len(t) > 1]
    return " ".join(tokens[:3]) if tokens else None


# --- format detection (proposals only) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormatDetection:
    """Proposed formats. If ``ambiguous`` on either side the user must choose."""

    date: DateFormatGuess
    number: NumberFormatGuess

    @property
    def needs_user_choice(self) -> bool:
        return (
            self.date.ambiguous
            or self.number.ambiguous
            or not self.date.candidates
            or not self.number.candidates
        )


def _cell(row: RawRow, index: int | None) -> str:
    if index is None or index >= len(row.cells):
        return ""
    return row.cells[index].strip()


def detect_formats(
    raw: RawTable, mapping: ColumnMapping, *, currency: str, sample_limit: int = 200
) -> FormatDetection:
    """Propose date and number formats from the first rows without deciding anything."""
    date_samples: list[str] = []
    amount_samples: list[str] = []
    amount_cols = [
        c
        for c in (mapping.amount_col, mapping.debit_col, mapping.credit_col, mapping.balance_col)
        if c is not None
    ]
    for row in raw.rows[:sample_limit]:
        if _cell(row, mapping.date_col):
            date_samples.append(_cell(row, mapping.date_col))
            amount_samples.extend(v for c in amount_cols if (v := _cell(row, c)))
    return FormatDetection(
        date=detect_date_format(date_samples),
        number=detect_number_format(amount_samples, currency=currency),
    )


# --- normalization --------------------------------------------------------------------------


def _check_mapping(raw: RawTable, mapping: ColumnMapping, choices: ImportChoices) -> None:
    width = max((len(r.cells) for r in raw.rows), default=0)
    for name, index in mapping.model_dump().items():
        if index is not None and raw.rows and index >= width:
            raise MappingError(f"mapped column '{name}' is outside the extracted table")
    convention = choices.sign_convention
    split = mapping.debit_col is not None or mapping.credit_col is not None
    if convention == SignConvention.DEBIT_CREDIT_COLUMNS and not split:
        raise MappingError("debit/credit convention needs debit and/or credit columns mapped")
    if convention != SignConvention.DEBIT_CREDIT_COLUMNS and split:
        raise MappingError("debit/credit columns are mapped but another convention was chosen")
    if convention == SignConvention.INDICATOR_DR_CR and mapping.indicator_col is None:
        raise MappingError("the DR/CR convention needs an indicator column mapped")
    validate_currency(choices.default_currency)


@dataclass(slots=True)
class _Draft:
    row: RawRow
    txn_date: date
    booking_date: date | None
    description: str
    amount: Money
    balance_minor: int | None
    amount_minor: int = field(init=False)

    def __post_init__(self) -> None:
        self.amount_minor = self.amount.minor


def normalize(
    raw: RawTable,
    mapping: ColumnMapping,
    choices: ImportChoices,
    *,
    import_batch_id: str,
    today: date | None = None,
    min_ocr_confidence: float = 80.0,
) -> NormalizationResult:
    """Normalize extracted rows into canonical transactions.

    Rejected rows are reported as ERROR issues and excluded. Blank rows, footer rows and
    description continuation lines are skipped or merged (reported as INFO). Identical rows in
    one file are all kept and distinguished by ``occurrence_index``.
    """
    _check_mapping(raw, mapping, choices)
    today = today or date.today()
    issues: list[ValidationIssue] = []
    drafts: list[_Draft] = []
    skipped = rejected = 0
    low_confidence: list[int] = []

    def issue(sev: Severity, code: str, msg: str, row: RawRow) -> None:
        issues.append(ValidationIssue(sev, code, msg, row.row_index))

    amount_cols = [
        c for c in (mapping.amount_col, mapping.debit_col, mapping.credit_col) if c is not None
    ]
    two_digit_year_flagged = has_two_digit_year(choices.date_format)

    for row in raw.rows:
        date_text = _cell(row, mapping.date_col)
        description = _cell(row, mapping.description_col)
        amounts_blank = not any(_cell(row, c) for c in amount_cols)

        if not date_text:
            if amounts_blank and not description:
                skipped += 1
                continue
            # Totals rows usually carry sums in the amount columns, so no amounts_blank check.
            if _FOOTER.match(description):
                issue(Severity.INFO, I_FOOTER_SKIPPED, "Footer or summary row skipped.", row)
                skipped += 1
                continue
            if amounts_blank and drafts:
                drafts[-1].description = f"{drafts[-1].description} {description}"
                issue(Severity.INFO, I_CONTINUATION_MERGED, "Continuation line merged.", row)
                skipped += 1
                continue
            issue(Severity.ERROR, E_MISSING_DATE, "Row has no date.", row)
            rejected += 1
            continue

        try:
            txn_date = parse_date(date_text, choices.date_format)
        except DateParseError:
            issue(Severity.ERROR, E_BAD_DATE, "Date does not match the selected format.", row)
            rejected += 1
            continue

        currency = choices.default_currency
        if mapping.currency_col is not None and _cell(row, mapping.currency_col):
            currency = _cell(row, mapping.currency_col).upper()
            try:
                validate_currency(currency)
            except ValueError:
                issue(Severity.ERROR, E_BAD_CURRENCY, "Currency code is not valid.", row)
                rejected += 1
                continue

        parsed = _parse_amount_for_row(row, mapping, choices, currency, issue)
        if parsed is None:
            rejected += 1
            continue

        booking: date | None = None
        if mapping.booking_date_col is not None and _cell(row, mapping.booking_date_col):
            try:
                booking = parse_date(_cell(row, mapping.booking_date_col), choices.date_format)
            except DateParseError:
                issue(Severity.WARNING, W_BAD_BOOKING_DATE, "Booking date ignored.", row)

        balance: int | None = None
        if mapping.balance_col is not None and _cell(row, mapping.balance_col):
            try:
                balance = parse_amount(
                    _cell(row, mapping.balance_col),
                    currency=currency,
                    decimal_sep=choices.decimal_sep,
                    thousands_sep=choices.thousands_sep,
                ).minor
            except AmountParseError:
                issue(Severity.WARNING, W_BAD_BALANCE, "Balance could not be read.", row)

        if two_digit_year_flagged:
            issue(Severity.WARNING, W_TWO_DIGIT_YEAR, "Two-digit year; verify the century.", row)

        drafts.append(_Draft(row, txn_date, booking, description, parsed, balance))
        if row.confidence is not None and row.confidence < min_ocr_confidence:
            low_confidence.append(row.row_index)
            issue(
                Severity.WARNING,
                W_LOW_OCR_CONFIDENCE,
                "Low OCR confidence: confirm the amount against the original.",
                row,
            )

    transactions: list[CanonicalTransaction] = []
    seen: dict[tuple[date, int, str, str], int] = {}
    now = utc_now_iso()
    for draft in drafts:
        clean, truncated = sanitize_text(draft.description)
        if truncated:
            issue(Severity.WARNING, W_DESCRIPTION_TRUNCATED, "Description truncated.", draft.row)
        normalized = normalize_description(clean)
        key = (draft.txn_date, draft.amount.minor, draft.amount.currency, normalized)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        txn = CanonicalTransaction(
            id=new_id(),
            account_id=choices.account_id,
            import_batch_id=import_batch_id,
            txn_date=draft.txn_date,
            booking_date=draft.booking_date,
            original_description=clean,
            normalized_description=normalized,
            merchant_key=merchant_key(normalized),
            amount_minor=draft.amount.minor,
            currency=draft.amount.currency,
            reported_balance_minor=draft.balance_minor,
            source_ref=SourceRef(
                file_sha256=raw.file_sha256,
                location=draft.row.location,
                row_index=draft.row.row_index,
            ),
            occurrence_index=occurrence,
            created_at=now,
            updated_at=now,
        )
        issues.extend(validate_transaction(txn, row_index=draft.row.row_index, today=today))
        transactions.append(txn)

    return NormalizationResult(
        transactions=tuple(transactions),
        issues=tuple(issues),
        rows_read=len(raw.rows),
        rows_skipped=skipped,
        rows_rejected=rejected,
        low_confidence_rows=tuple(low_confidence),
    )


def _parse_amount_for_row(
    row: RawRow,
    mapping: ColumnMapping,
    choices: ImportChoices,
    currency: str,
    issue: Callable[[Severity, str, str, RawRow], None],
) -> Money | None:
    """Return the signed amount (inflow positive) or None after recording an ERROR issue."""

    def parse(text: str) -> Money:
        return parse_amount(
            text,
            currency=currency,
            decimal_sep=choices.decimal_sep,
            thousands_sep=choices.thousands_sep,
        )

    convention = choices.sign_convention
    try:
        if convention == SignConvention.DEBIT_CREDIT_COLUMNS:
            debit_text, credit_text = _cell(row, mapping.debit_col), _cell(row, mapping.credit_col)
            debit = parse(debit_text) if debit_text else Money.zero(currency)
            credit = parse(credit_text) if credit_text else Money.zero(currency)
            if not debit_text and not credit_text:
                issue(Severity.ERROR, E_NO_AMOUNT, "Row has no amount.", row)
                return None
            if debit.minor < 0 or credit.minor < 0:
                issue(
                    Severity.WARNING,
                    W_SIGN_IN_DEBIT_CREDIT,
                    "A sign in a debit/credit column was ignored.",
                    row,
                )
            if debit.minor != 0 and credit.minor != 0:
                issue(Severity.ERROR, E_BOTH_DEBIT_CREDIT, "Both debit and credit are set.", row)
                return None
            return abs(credit) - abs(debit)

        text = _cell(row, mapping.amount_col)
        if not text:
            issue(Severity.ERROR, E_NO_AMOUNT, "Row has no amount.", row)
            return None
        amount = parse(text)
        if convention == SignConvention.NEGATIVE_IS_OUTFLOW:
            return amount
        if convention == SignConvention.POSITIVE_IS_OUTFLOW:
            return -amount
        indicator = _cell(row, mapping.indicator_col).upper()
        if indicator in _DEBIT_INDICATORS:
            return -abs(amount)
        if indicator in _CREDIT_INDICATORS:
            return abs(amount)
        issue(Severity.ERROR, E_BAD_INDICATOR, "DR/CR indicator not recognised.", row)
        return None
    except AmountParseError:
        issue(Severity.ERROR, E_BAD_AMOUNT, "Amount could not be read unambiguously.", row)
        return None
