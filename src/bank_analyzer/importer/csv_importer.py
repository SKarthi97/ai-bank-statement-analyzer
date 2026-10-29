"""CSV extraction: encoding, delimiter and header detection into a RawTable of strings.

This module never interprets money or dates. Detections that could be wrong (encoding fallback,
header row) are reported as warnings so the preview can show them.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from bank_analyzer.domain.errors import ImportStructureError
from bank_analyzer.domain.imports import RawRow, RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.column_mapper import find_header_row

MAX_ROWS = 200_000
_DELIMITERS = (",", ";", "\t", "|")
_ENCODINGS = ("utf-8", "cp1252")


def decode_text(data: bytes, encoding: str | None = None) -> tuple[str, str]:
    """Decode bytes to text, returning ``(text, encoding_used)``.

    BOMs are honoured (UTF-8, UTF-16). Otherwise UTF-8 is tried, then Windows-1252, then
    Latin-1 as a last resort. An explicit ``encoding`` overrides detection.
    """
    if encoding:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError) as exc:
            raise ImportStructureError("The file could not be decoded with that encoding.") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for name in _ENCODINGS:
        try:
            return data.decode(name), name
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def detect_delimiter(text: str) -> str:
    """Pick the delimiter that splits the sampled lines into a consistent number of fields."""
    lines = [ln for ln in text.splitlines() if ln.strip()][:50]
    best: tuple[float, int, str] | None = None
    for delim in _DELIMITERS:
        try:
            lengths = [len(r) for r in csv.reader(lines, delimiter=delim)]
        except csv.Error as exc:
            raise ImportStructureError(
                "The CSV file is malformed or has an oversized field."
            ) from exc
        if not lengths:
            continue
        modal = max(set(lengths), key=lengths.count)
        coverage = lengths.count(modal) / len(lengths)
        if modal >= 2 and coverage >= 0.6 and (best is None or (coverage, modal) > best[:2]):
            best = (coverage, modal, delim)
    if best is None:
        raise ImportStructureError(
            "Could not determine the CSV delimiter. The file may not be a table."
        )
    return best[2]


def read_csv(
    path: Path,
    *,
    sha256: str,
    encoding: str | None = None,
    delimiter: str | None = None,
    header_row: int | None = None,
    max_rows: int = MAX_ROWS,
) -> RawTable:
    """Extract a CSV file into a RawTable.

    ``header_row`` is the 1-based record number of the header (to override detection). Row
    numbers in the result are 1-based record numbers in the file, so users can find them.
    """
    text, used_encoding = decode_text(path.read_bytes(), encoding)
    delim = delimiter or detect_delimiter(text)
    try:
        records = [
            [c.strip() for c in rec]
            for rec in csv.reader(io.StringIO(text, newline=""), delimiter=delim)
        ]
    except csv.Error as exc:
        raise ImportStructureError("The CSV file is malformed.") from exc

    warnings: list[str] = []
    if used_encoding not in {"utf-8", "utf-8-sig"}:
        warnings.append(
            f"Text was decoded as {used_encoding}; check that accents and symbols look right."
        )
    warnings.append(f"Delimiter {delim!r} detected.")

    if header_row is not None:
        if not 1 <= header_row <= len(records):
            raise ImportStructureError("The chosen header row is outside the file.")
        header_index: int | None = header_row - 1
    else:
        header_index = find_header_row(records)
    if header_index is None:
        raise ImportStructureError("No header row was recognised. Choose the header row manually.")
    if header_index > 0:
        warnings.append(f"Skipped {header_index} line(s) before the header row.")

    headers = tuple(records[header_index])
    rows: list[RawRow] = []
    blank = 0
    for number, cells in enumerate(records[header_index + 1 :], start=header_index + 2):
        if not any(cells):
            blank += 1
            continue
        if len(rows) >= max_rows:
            raise ImportStructureError(f"The file has more than {max_rows:,} rows.")
        rows.append(RawRow(row_index=number, cells=tuple(cells)))
    if blank:
        warnings.append(f"Ignored {blank} blank row(s).")
    return RawTable(
        file_sha256=sha256,
        file_kind=FileKind.CSV,
        headers=headers,
        rows=tuple(rows),
        warnings=tuple(warnings),
    )
