"""CSV export that cannot run formulas when opened in a spreadsheet.

Spreadsheet programs treat a cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage
return as a formula, and statement descriptions are untrusted text. Every text cell is therefore
prefixed with an apostrophe when its first visible character is one of those, which makes the cell
plain text. Amounts are written from ``Money`` as plain decimals and are never prefixed: a negative
amount such as ``-85.40`` must remain a number. The file is UTF-8 with a byte-order mark so Excel
reads accented text correctly.

The apostrophe stays visible in some tools (it is a marker in Excel but a literal character in
others). That is the accepted cost of the standard defence.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping

from bank_analyzer.domain.models import CanonicalTransaction, Category

_TRIGGERS = frozenset("=+-@\t\r\n")
COLUMNS = (
    "Date",
    "Account",
    "Description",
    "Amount",
    "Currency",
    "Category",
    "Review status",
    "Transaction ID",
)


def neutralize_cell(value: str) -> str:
    """Prefix ``'`` if the cell would be read as a formula (first non-space character is a trigger)."""
    stripped = value.lstrip(" ")
    if value and (value[0] in _TRIGGERS or (stripped and stripped[0] in _TRIGGERS)):
        return "'" + value
    return value


def transactions_csv(
    txns: Iterable[CanonicalTransaction],
    categories: Mapping[str, Category],
    account_names: Mapping[str, str] | None = None,
) -> bytes:
    """Transactions as CSV bytes (UTF-8 with BOM). Text cells are formula-neutralized."""
    names = account_names or {}
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for t in txns:
        category = categories.get(t.category_id) if t.category_id else None
        writer.writerow(
            [
                t.txn_date.isoformat(),
                neutralize_cell(names.get(t.account_id, "")),
                neutralize_cell(t.original_description),
                format(t.amount.to_decimal(), "f"),
                t.currency,
                neutralize_cell(category.name if category else ""),
                t.review_status.value,
                t.id,
            ]
        )
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
