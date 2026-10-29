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

from bank_analyzer.domain.errors import FileValidationError, ImportStructureError, NeedsOcrError
from bank_analyzer.domain.imports import RawRow, RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.column_mapper import find_header_row, normalize_header

DEFAULT_MAX_PAGES = 100
_MIN_COLUMNS = 3
_MIN_TEXT_CHARS = 20
_DATE_LIKE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s[A-Za-z]{3,9}\s\d{4})\b"
)
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


def _page_tables(page: Any) -> tuple[list[list[list[str]]], str]:
    """Best table set for one page and the strategy that produced it."""
    ruled = [t for t in page.extract_tables() if t]
    if _score(ruled) >= 1:
        return [[[_clean(c) for c in row] for row in t] for t in ruled], "ruled"
    aligned = [t for t in page.extract_tables(_TEXT_STRATEGY) if t]
    if _score(aligned) >= 1:
        return [[[_clean(c) for c in row] for row in t] for t in aligned], "text"
    return [], "none"


def read_pdf(path: Path, *, sha256: str, max_pages: int = DEFAULT_MAX_PAGES) -> RawTable:
    """Extract the transaction table(s) of a text-based PDF into a RawTable.

    Row numbers are the 1-based position of the row within its extracted table on that page and
    ``location`` is ``"page N"``. Repeated header rows on later pages are dropped. Tables whose
    column count differs from the header are skipped with a warning rather than misaligned.
    """
    try:
        pdf = pdfplumber.open(path)
    except PdfminerException as exc:
        # pdfplumber wraps pdfminer's errors; the original is the first argument.
        if exc.args and isinstance(exc.args[0], PDFPasswordIncorrect):
            raise ImportStructureError(
                "The PDF is password-protected. Remove the protection and try again."
            ) from exc
        raise ImportStructureError("The PDF could not be read.") from exc
    except Exception as exc:  # third-party parser boundary: any failure means unreadable
        raise ImportStructureError("The PDF could not be read.") from exc

    try:
        pages = pdf.pages
        if len(pages) > max_pages:
            raise FileValidationError(f"The PDF has more than {max_pages} pages.")

        headers: tuple[str, ...] | None = None
        rows: list[RawRow] = []
        text_chars = 0
        strategies = {"ruled": 0, "text": 0}
        skipped_tables = 0
        fragmented_tables = 0
        for page_number, page in enumerate(pages, start=1):
            try:
                text_chars += len(page.extract_text() or "")
                tables, strategy = _page_tables(page)
            except Exception as exc:  # third-party parser boundary
                raise ImportStructureError(f"Page {page_number} could not be read.") from exc
            if strategy != "none":
                strategies[strategy] += 1
            for table in tables:
                header_at = find_header_row(table)
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
    )


def _same_header(cells: Sequence[str], headers: Sequence[str]) -> bool:
    return [normalize_header(c) for c in cells] == [normalize_header(h) for h in headers]
