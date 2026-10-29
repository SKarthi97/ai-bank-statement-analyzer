"""Turning stored data into display rows.

Statement-derived text is untrusted. Pages must show it with ``st.text`` / ``st.dataframe`` (which
do not interpret markdown or HTML), never through markdown or ``unsafe_allow_html``. ``safe_cell``
additionally removes control characters and bounds the length, so a hostile description cannot
break a table or flood the screen.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence

from bank_analyzer.database.repositories import Page
from bank_analyzer.domain.models import CanonicalTransaction, Category

MAX_CELL = 120


def safe_cell(text: str | None, limit: int = MAX_CELL) -> str:
    """Printable, single-line, length-bounded text."""
    if not text:
        return ""
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def transaction_rows(
    txns: Sequence[CanonicalTransaction], categories: Mapping[str, Category]
) -> list[dict[str, str]]:
    """Rows for a transactions table. The id is kept so a row can be acted on."""
    rows = []
    for t in txns:
        category = categories.get(t.category_id) if t.category_id else None
        rows.append(
            {
                "Date": t.txn_date.isoformat(),
                "Description": safe_cell(t.normalized_description),
                "Amount": t.amount.format(),
                "Category": category.name if category else "",
                "Status": t.review_status.value.replace("_", " "),
                "Source": t.category_source.value.replace("_", " "),
                "Transfer": "linked" if t.transfer_group_id else "",
                "ID": t.id,
            }
        )
    return rows


def page_summary(page: Page[CanonicalTransaction]) -> str:
    if page.total == 0:
        return "No transactions match."
    first = (page.page - 1) * page.page_size + 1
    last = first + len(page.items) - 1
    return f"Showing {first}-{last} of {page.total}"
