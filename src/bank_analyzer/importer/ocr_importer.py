"""Optional OCR for scanned PDFs using a locally installed Tesseract.

Everything stays on the machine: pages are rendered to private temporary PNGs and Tesseract is
run as a subprocess (argument list, no shell, timeout) that only reads those images. OCR can
misread digits, so this path is deliberately conservative:

* every row carries the lowest word confidence found in it, and low-confidence rows must be
  confirmed by the user before import (see ``normalize`` and the import service);
* rows whose number of cells differs from the header are *left out* with a warning, because a
  missing cell (for example an empty debit column) would shift amounts into the wrong column
  and silently flip a sign.

The TSV parsing and table building are pure functions that are unit-tested without Tesseract.
The subprocess call itself needs a real Tesseract install and is covered only by an optional
``ocr``-marked test.
"""

from __future__ import annotations

import csv
import io
import shutil
import statistics
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

from bank_analyzer.domain.errors import FileValidationError, ImportStructureError
from bank_analyzer.domain.imports import RawRow, RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.column_mapper import find_header_row

DEFAULT_DPI = 200
DEFAULT_TIMEOUT_S = 120
_GAP_FACTOR = 1.2  # a horizontal gap wider than this many text heights starts a new cell
_MIN_GAP_PX = 8


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One recognised word with its position (pixels) and confidence (0-100)."""

    left: int
    top: int
    width: int
    height: int
    confidence: float
    text: str
    line_key: tuple[int, int, int]  # (block, paragraph, line)


def parse_tsv(tsv: str) -> list[OcrWord]:
    """Parse Tesseract ``tsv`` output into words, skipping structural and empty entries."""
    words: list[OcrWord] = []
    for record in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        try:
            if int(record["level"]) != 5:
                continue
            text = (record.get("text") or "").strip()
            confidence = float(record["conf"])
            if not text or confidence < 0:
                continue
            words.append(
                OcrWord(
                    left=int(record["left"]),
                    top=int(record["top"]),
                    width=int(record["width"]),
                    height=int(record["height"]),
                    confidence=confidence,
                    text=text,
                    line_key=(
                        int(record["block_num"]),
                        int(record["par_num"]),
                        int(record["line_num"]),
                    ),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue  # a malformed record is dropped, never guessed
    return words


@dataclass(frozen=True, slots=True)
class OcrRow:
    cells: tuple[str, ...]
    confidence: float


def words_to_rows(words: Sequence[OcrWord]) -> list[OcrRow]:
    """Group words into lines (top to bottom) and split each line into cells by wide gaps."""
    lines: dict[tuple[int, int, int], list[OcrWord]] = {}
    for word in words:
        lines.setdefault(word.line_key, []).append(word)
    ordered = sorted(
        lines.values(), key=lambda ws: (min(w.top for w in ws), min(w.left for w in ws))
    )

    rows: list[OcrRow] = []
    for line in ordered:
        line = sorted(line, key=lambda w: w.left)
        threshold = max(_MIN_GAP_PX, _GAP_FACTOR * statistics.median(w.height for w in line))
        cells: list[list[OcrWord]] = [[line[0]]]
        for previous, current in zip(line, line[1:], strict=False):
            gap = current.left - (previous.left + previous.width)
            if gap > threshold:
                cells.append([current])
            else:
                cells[-1].append(current)
        rows.append(
            OcrRow(
                cells=tuple(" ".join(w.text for w in cell) for cell in cells),
                confidence=min(w.confidence for w in line),
            )
        )
    return rows


def build_table(
    pages: Sequence[Sequence[OcrRow]], *, sha256: str, min_confidence: float = 80.0
) -> RawTable:
    """Turn OCR rows (one list per page) into a RawTable, never guessing misaligned cells."""
    headers: tuple[str, ...] | None = None
    rows: list[RawRow] = []
    ragged = 0
    for page_number, page_rows in enumerate(pages, start=1):
        start = 0
        if headers is None:
            at = find_header_row([r.cells for r in page_rows])
            if at is None:
                continue
            headers = page_rows[at].cells
            start = at + 1
        for position, row in enumerate(page_rows[start:], start=start + 1):
            if len(row.cells) != len(headers):
                ragged += 1
                continue
            rows.append(
                RawRow(
                    row_index=position,
                    cells=row.cells,
                    location=f"page {page_number}",
                    confidence=row.confidence,
                )
            )
    if headers is None or not rows:
        raise ImportStructureError(
            "OCR could not find a transaction table. The scan may be too faint or skewed. "
            "Export a CSV from your bank if possible."
        )
    warnings = [
        "This document was read with OCR. Digits can be misread: check every amount against "
        "the original before importing.",
    ]
    low = sum(1 for r in rows if r.confidence is not None and r.confidence < min_confidence)
    if low:
        warnings.append(
            f"{low} row(s) have low OCR confidence (below {min_confidence:g}) and must be "
            "confirmed individually."
        )
    if ragged:
        warnings.append(
            f"{ragged} line(s) had a different number of columns than the header and were left "
            "out, because missing cells would shift amounts into the wrong column. "
            "Some transactions may be missing."
        )
    return RawTable(
        file_sha256=sha256,
        file_kind=FileKind.PDF_OCR,
        headers=headers,
        rows=tuple(rows),
        warnings=tuple(warnings),
    )


def find_tesseract(command: str | None = None) -> str | None:
    """Resolve the Tesseract executable (explicit path/name, else PATH), or None."""
    return shutil.which(command or "tesseract")


def run_tesseract(command: str, image: Path, *, timeout: int = DEFAULT_TIMEOUT_S) -> str:
    """Run Tesseract on one image and return its TSV output. No shell is involved."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            # psm 4 (a column of variable-size text) read the ruled fixture fully; psm 6 returned only
            # its title line under Tesseract 5 (found by the first real OCR run).
            [command, str(image), "stdout", "--psm", "4", "tsv"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImportStructureError("OCR took too long and was stopped.") from exc
    except OSError as exc:
        raise ImportStructureError("OCR could not be started.") from exc
    if completed.returncode != 0:
        raise ImportStructureError("OCR failed on a page.")
    return completed.stdout.decode("utf-8", errors="replace")


Runner = Callable[[Path], str]  # image path -> Tesseract TSV text


def read_pdf_ocr(
    path: Path,
    *,
    sha256: str,
    tesseract_cmd: str | None = None,
    max_pages: int = 100,
    dpi: int = DEFAULT_DPI,
    min_confidence: float = 80.0,
    runner: Runner | None = None,
) -> RawTable:
    """OCR a scanned PDF into a RawTable. Requires a local Tesseract (or an injected runner)."""
    run = runner
    if run is None:
        command = find_tesseract(tesseract_cmd)
        if command is None:
            raise ImportStructureError(
                "OCR is not available: Tesseract is not installed or not on the PATH."
            )

        def run(image: Path) -> str:
            return run_tesseract(command, image)

    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # third-party parser boundary
        raise ImportStructureError("The PDF could not be read.") from exc
    pages_rows: list[list[OcrRow]] = []
    workdir = Path(tempfile.mkdtemp(prefix="bankan-ocr-"))
    try:
        if len(pdf.pages) > max_pages:
            raise FileValidationError(f"The PDF has more than {max_pages} pages.")
        for number, page in enumerate(pdf.pages, start=1):
            image_path = workdir / f"page{number}.png"
            page.to_image(resolution=dpi).original.save(image_path)
            pages_rows.append(words_to_rows(parse_tsv(run(image_path))))
            image_path.unlink(missing_ok=True)
    finally:
        pdf.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return build_table(pages_rows, sha256=sha256, min_confidence=min_confidence)
