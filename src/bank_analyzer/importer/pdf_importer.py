"""Text-based PDF extraction with pdfplumber.

Only tables that can actually be detected on the page are returned; rows are never reconstructed
by guessing from loose text. Two detection strategies are tried per page: ruled tables (drawn
lines) first, then text-alignment. Pages that fall back to text alignment are reported in the
warnings because column boundaries are less certain. A PDF with no text layer is a scan and
raises ``NeedsOcrError`` so the caller can offer the optional OCR path.

Nothing in the PDF is executed: pdfplumber/pdfminer only parse content streams. Parsing runs
in-process; running it in a resource-limited subprocess is planned for Phase 7.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from bank_analyzer.domain.errors import (
    FileValidationError,
    ImportStructureError,
    NeedsOcrError,
    PasswordRequiredError,
    WrongPasswordError,
)
from bank_analyzer.domain.imports import RawRow, RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.column_mapper import find_header_row, normalize_header

DEFAULT_MAX_PAGES = 100
_MIN_COLUMNS = 3
_MIN_TEXT_CHARS = 20
_MONTH_NAME = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
# Full dates, and dates without a year ("28/10", "28 Oct"): many statements print the year once in
# the heading. "5.00" is deliberately not a date, so a dot is only accepted together with a year.
_DATE_LIKE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s[A-Za-z]{3,9}\s\d{4}"
    rf"|\d{{1,2}}[/-]\d{{1,2}}|\d{{1,2}}\s{_MONTH_NAME})\b",
    re.IGNORECASE,
)
_CONTEXT_CHARS = 1500
_TEXT_STRATEGY: dict[str, Any] = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def _clean(cell: object) -> str:
    return " ".join(str(cell).split()) if cell is not None else ""


def _score(tables: Sequence[Sequence[Sequence[object]]]) -> int:
    """Number of wide rows containing a date-like cell: a proxy for 'this is a statement table'."""
    return sum(
        1
        for table in tables
        for row in table
        if len(row) >= _MIN_COLUMNS and any(_DATE_LIKE.search(_clean(c)) for c in row)
    )


_MIN_GUTTER_PT = 5.0  # blank width that separates two columns (a word space is about 2.5 pt)
_MAX_HEADER_WORD_GAP_PT = 12.0  # a wider gap inside one heading cell means two columns were merged
_LINE_TOLERANCE_PT = 3.0


def _group_lines(words: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if lines and abs(float(word["top"]) - float(lines[-1][0]["top"])) <= _LINE_TOLERANCE_PT:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: float(w["x0"])) for line in lines]


def _bands(words: Sequence[dict[str, Any]]) -> list[tuple[float, float]]:
    """Column bands: x-ranges separated by blank gutters that no word in the table crosses."""
    merged: list[list[float]] = []
    for x0, x1 in sorted((float(w["x0"]), float(w["x1"])) for w in words):
        if merged and x0 - merged[-1][1] < _MIN_GUTTER_PT:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    return [(a, b) for a, b in merged]


def _band_cells(line: Sequence[dict[str, Any]], bands: Sequence[tuple[float, float]]) -> list[str]:
    cells: list[str] = []
    for low, high in bands:
        inside = [w for w in line if low <= (float(w["x0"]) + float(w["x1"])) / 2 <= high]
        cells.append(" ".join(str(w["text"]) for w in inside))
    return cells


def _is_dated_line(line: Sequence[dict[str, Any]]) -> bool:
    start = " ".join(str(w["text"]) for w in line[:3])
    return _DATE_LIKE.match(start) is not None


def _words_table(page: Any) -> list[list[str]] | None:
    """A table built from word positions: robust to shading and to sparse columns.

    Columns come from the vertical gutters across the whole table, so a column with only a couple
    of entries (typical for payments or receipts) is still its own column. Returns None when the
    page does not give an unambiguous table: no heading line, a nameless column, or a heading
    cell that spans two columns.
    """
    words = page.extract_words()
    if not words:
        return None
    lines = _group_lines(words)
    dated = [i for i, line in enumerate(lines) if _is_dated_line(line)]
    if len(dated) < 2 or dated[0] == 0:
        return None
    region = lines[dated[0] - 1 : dated[-1] + 1]
    bands = _bands([w for line in region for w in line])
    if len(bands) < _MIN_COLUMNS:
        return None
    header = _band_cells(region[0], bands)
    if any(not cell for cell in header):
        return None
    for low, high in bands:
        inside = [w for w in region[0] if low <= (float(w["x0"]) + float(w["x1"])) / 2 <= high]
        if any(
            float(b["x0"]) - float(a["x1"]) > _MAX_HEADER_WORD_GAP_PT
            for a, b in zip(inside, inside[1:], strict=False)
        ):
            return None
    table = [[_clean(c) for c in _band_cells(line, bands)] for line in region]
    return [row for row in table if any(row)]


def _page_tables(page: Any) -> tuple[list[list[list[str]]], str]:
    """Best table set for one page and the strategy that produced it."""
    ruled = [t for t in page.extract_tables() if t]
    if _score(ruled) >= 1:
        return [[[_clean(c) for c in row] for row in t] for t in ruled], "ruled"
    by_words = _words_table(page)
    if by_words is not None:
        return [by_words], "text"
    aligned = [t for t in page.extract_tables(_TEXT_STRATEGY) if t]
    if _score(aligned) >= 1:
        return [[[_clean(c) for c in row] for row in t] for t in aligned], "text"
    return [], "none"


def open_pdf(path: Path, password: str | None = None) -> Any:
    """Open a PDF, with the password if it has one. The password is never kept or put in a message."""
    try:
        return pdfplumber.open(path, password=password or None)
    except PdfminerException as exc:
        # pdfplumber wraps pdfminer's errors; the original is the first argument.
        if exc.args and isinstance(exc.args[0], PDFPasswordIncorrect):
            if password:
                raise WrongPasswordError("That password does not open the PDF.") from exc
            raise PasswordRequiredError(
                "The PDF is password-protected. Enter its password and read the file again."
            ) from exc
        raise ImportStructureError("The PDF could not be read.") from exc
    except Exception as exc:  # third-party parser boundary: any failure means unreadable
        raise ImportStructureError("The PDF could not be read.") from exc


def read_pdf(
    path: Path, *, sha256: str, max_pages: int = DEFAULT_MAX_PAGES, password: str | None = None
) -> RawTable:
    """Extract the transaction table(s) of a text-based PDF into a RawTable.

    Row numbers are the 1-based position of the row within its extracted table on that page and
    ``location`` is ``"page N"``. Repeated header rows on later pages are dropped. Tables whose
    column count differs from the header are skipped with a warning rather than misaligned.
    """
    pdf = open_pdf(path, password)

    try:
        pages = pdf.pages
        page_count = len(pages)
        if len(pages) > max_pages:
            raise FileValidationError(f"The PDF has more than {max_pages} pages.")

        headers: tuple[str, ...] | None = None
        rows: list[RawRow] = []
        text_chars = 0
        first_text = last_text = ""
        guessed_header = False
        strategies = {"ruled": 0, "text": 0}
        skipped_tables = 0
        fragmented_tables = 0
        for page_number, page in enumerate(pages, start=1):
            try:
                page_text = page.extract_text() or ""
                text_chars += len(page_text)
                first_text = first_text or page_text
                last_text = page_text
                tables, strategy = _page_tables(page)
            except Exception as exc:  # third-party parser boundary
                raise ImportStructureError(f"Page {page_number} could not be read.") from exc
            if strategy != "none":
                strategies[strategy] += 1
            for table in tables:
                header_at = find_header_row(table)
                if header_at is None and headers is None:
                    header_at = _guess_header_row(table)
                    guessed_header = guessed_header or header_at is not None
                # Text-aligned tables whose header has unlabelled columns are fragmented: the
                # detector split cells (even words) at arbitrary positions. Using them would
                # silently truncate descriptions or misplace amounts, so they are refused.
                if (
                    strategy == "text"
                    and header_at is not None
                    and any(not cell for cell in table[header_at])
                ):
                    fragmented_tables += 1
                    continue
                if headers is None:
                    if header_at is None:
                        continue  # a table before the header was seen: not the statement table
                    headers = tuple(table[header_at])
                    start = header_at + 1
                elif header_at is not None and _same_header(table[header_at], headers):
                    start = header_at + 1
                else:
                    start = 0
                width = max((len(r) for r in table), default=0)
                if width != len(headers):
                    skipped_tables += 1
                    continue
                for position, cells in enumerate(table[start:], start=start + 1):
                    if any(cells):
                        rows.append(
                            RawRow(
                                row_index=position,
                                cells=tuple(cells),
                                location=f"page {page_number}",
                            )
                        )
    finally:
        pdf.close()

    if headers is None or not rows:
        if fragmented_tables:
            raise ImportStructureError(
                "The columns in this PDF are not drawn as a ruled table and cannot be read "
                "reliably (cells were split at arbitrary positions). Export a CSV or XLSX "
                "from your bank instead."
            )
        if text_chars < _MIN_TEXT_CHARS:
            raise NeedsOcrError(
                "This PDF has no readable text, so it is probably a scan. "
                "Use OCR (optional) or export the statement as CSV from your bank."
            )
        raise ImportStructureError(
            "No transaction table was found in the PDF. Export a CSV or XLSX from your bank, "
            "or try OCR if the PDF is a scan."
        )

    warnings = [
        "Columns were detected from the PDF layout. Check every column in the preview "
        "before importing."
    ]
    if guessed_header:
        warnings.append(
            "The column headings were not ones this app knows, so the row above the first "
            "dated row was used as the heading. Check which column is which."
        )
    if strategies["text"]:
        warnings.append(
            f"{strategies['text']} page(s) had no ruled table and were read by text alignment; "
            "column boundaries are less certain there."
        )
    if skipped_tables:
        warnings.append(
            f"{skipped_tables} table(s) with a different number of columns than the header "
            "were skipped to avoid misaligned amounts. Some transactions may be missing."
        )
    return RawTable(
        file_sha256=sha256,
        file_kind=FileKind.PDF,
        headers=headers,
        rows=tuple(rows),
        warnings=tuple(warnings),
        context_text=_context(first_text, last_text, pages=page_count),
    )


def _context(first: str, last: str, *, pages: int) -> str:
    """The top of the first page and the bottom of the last: header details and footer totals."""
    if pages == 1:
        return first[: 2 * _CONTEXT_CHARS]
    return "\n".join((first[:_CONTEXT_CHARS], "...", last[-_CONTEXT_CHARS:]))


def _guess_header_row(table: Sequence[Sequence[str]]) -> int | None:
    """The row just above the first dated row, if it is a plausible heading (3+ labelled cells).

    Used only when no heading of known words was found. The result is still tested against the
    document's own balances before a layout built on it is trusted.
    """
    for index in range(min(len(table) - 1, 6)):
        row, following = table[index], table[index + 1]
        if (
            not any(_DATE_LIKE.search(cell) for cell in row)
            and any(_DATE_LIKE.search(cell) for cell in following)
            and sum(1 for cell in row if cell) >= _MIN_COLUMNS
        ):
            return index
    return None


def _same_header(cells: Sequence[str], headers: Sequence[str]) -> bool:
    return [normalize_header(c) for c in cells] == [normalize_header(h) for h in headers]
