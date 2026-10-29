"""Header detection and column-mapping suggestions.

Everything here is a *proposal with evidence*. Suggestions are never applied silently: the
caller shows them and the user confirms or edits the mapping, date format and sign convention.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from bank_analyzer.domain.errors import AmountParseError
from bank_analyzer.domain.imports import ColumnMapping, RawTable, SignConvention
from bank_analyzer.domain.models import AccountType
from bank_analyzer.domain.money import parse_amount

_VOCAB: dict[str, frozenset[str]] = {
    "date": frozenset(
        {"date", "transaction date", "trans date", "txn date", "posting date", "post date"}
        | {"value date", "datum", "buchungstag", "fecha"}
    ),
    "booking_date": frozenset({"booking date", "book date", "posted", "settlement date"}),
    "description": frozenset(
        {"description", "details", "narrative", "memo", "payee", "reference", "particulars"}
        | {"transaction details", "beschreibung", "verwendungszweck", "text", "merchant"}
        | {"transaction", "name"}
    ),
    "amount": frozenset({"amount", "amt", "transaction amount", "betrag", "value"}),
    "debit": frozenset(
        {"debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "soll"}
        | {"debit amount", "dr"}
    ),
    "credit": frozenset(
        {"credit", "credits", "deposit", "deposits", "money in", "paid in", "haben"}
        | {"credit amount", "cr"}
    ),
    "balance": frozenset({"balance", "running balance", "closing balance", "saldo"}),
    "currency": frozenset({"currency", "ccy", "cur", "währung"}),
    "indicator": frozenset({"dr cr", "cr dr", "debit credit", "dc", "d c", "indicator"}),
}


def normalize_header(text: str) -> str:
    """Lower-case a header and reduce punctuation to single spaces."""
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def header_score(cells: Sequence[str]) -> int:
    """How many cells look like known statement column headers."""
    all_terms = frozenset().union(*_VOCAB.values())
    return sum(1 for c in cells if normalize_header(c) in all_terms)


def find_header_row(rows: Sequence[Sequence[str]], *, limit: int = 50) -> int | None:
    """Index (0-based) of the first row in ``rows[:limit]`` that has at least two known headers."""
    for index, cells in enumerate(rows[:limit]):
        if header_score(cells) >= 2:
            return index
    return None


@dataclass(frozen=True, slots=True)
class MappingSuggestion:
    """A proposed mapping, or ``None`` when it cannot be proposed without guessing."""

    mapping: ColumnMapping | None
    found: dict[str, int]
    notes: tuple[str, ...]


def suggest_mapping(headers: Sequence[str]) -> MappingSuggestion:
    """Propose a ColumnMapping from header names (leftmost match per field)."""
    found: dict[str, int] = {}
    for index, header in enumerate(headers):
        norm = normalize_header(header)
        for field, terms in _VOCAB.items():
            if norm in terms and field not in found:
                found[field] = index
                break

    notes: list[str] = []
    if "date" not in found:
        notes.append("No date column recognised.")
    if "description" not in found:
        notes.append("No description column recognised.")
    has_amount = "amount" in found
    has_split = "debit" in found or "credit" in found
    if not has_amount and not has_split:
        notes.append("No amount, debit or credit column recognised.")
    if has_amount and has_split:
        notes.append("Both an amount column and debit/credit columns were found. Choose one style.")
    if notes:
        return MappingSuggestion(None, found, tuple(notes))

    mapping = ColumnMapping(
        date_col=found["date"],
        description_col=found["description"],
        amount_col=found.get("amount"),
        debit_col=found.get("debit"),
        credit_col=found.get("credit"),
        indicator_col=found.get("indicator") if has_amount else None,
        currency_col=found.get("currency"),
        balance_col=found.get("balance"),
        booking_date_col=found.get("booking_date"),
    )
    return MappingSuggestion(mapping, found, ())


@dataclass(frozen=True, slots=True)
class SignHint:
    """Evidence about the sign convention of a single amount column."""

    convention: SignConvention | None
    consistent_pairs: int
    total_pairs: int
    note: str


def infer_sign_convention(
    raw: RawTable,
    mapping: ColumnMapping,
    *,
    currency: str,
    decimal_sep: str,
    thousands_sep: str | None,
    account_type: AccountType,
) -> SignHint:
    """Use the statement's running balance to suggest how the amount column is signed.

    For a deposit account the balance moves by the cash-flow amount; for a credit card or loan
    the balance is the amount owed, which moves the opposite way. The hint is only given when
    *every* consecutive pair of rows agrees, in listed or newest-first order. Otherwise there is
    no hint and the user must choose. The result is evidence, not a decision.
    """
    if mapping.amount_col is None or mapping.balance_col is None:
        return SignHint(None, 0, 0, "A single amount column and a balance column are needed.")

    series: list[tuple[int, int]] = []
    for row in raw.rows:
        try:
            amount = parse_amount(
                row.cells[mapping.amount_col],
                currency=currency,
                decimal_sep=decimal_sep,
                thousands_sep=thousands_sep,
            ).minor
            balance = parse_amount(
                row.cells[mapping.balance_col],
                currency=currency,
                decimal_sep=decimal_sep,
                thousands_sep=thousands_sep,
            ).minor
        except (AmountParseError, IndexError):
            continue
        series.append((amount, balance))
    if len(series) < 3:
        return SignHint(None, 0, 0, "Too few rows with both an amount and a balance.")

    liability = account_type in (AccountType.CREDIT_CARD, AccountType.LOAN)
    for ordered, label in ((series, "listed order"), (series[::-1], "newest-first order")):
        pairs = list(zip(ordered, ordered[1:], strict=False))
        same = sum(1 for (_, b0), (a1, b1) in pairs if b1 - b0 == a1)
        opposite = sum(1 for (_, b0), (a1, b1) in pairs if b1 - b0 == -a1)
        total = len(pairs)
        if same == total and opposite == total:
            continue  # all amounts zero: no information
        if same == total:
            convention = (
                SignConvention.POSITIVE_IS_OUTFLOW
                if liability
                else SignConvention.NEGATIVE_IS_OUTFLOW
            )
        elif opposite == total:
            convention = (
                SignConvention.NEGATIVE_IS_OUTFLOW
                if liability
                else SignConvention.POSITIVE_IS_OUTFLOW
            )
        else:
            continue
        return SignHint(convention, total, total, f"All {total} balance steps agree ({label}).")
    return SignHint(
        None, 0, len(series) - 1, "The balance column does not reconcile with the amounts."
    )
