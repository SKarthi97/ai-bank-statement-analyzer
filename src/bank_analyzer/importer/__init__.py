"""Statement extraction. Importers return text-only RawTables and never interpret values."""

from __future__ import annotations

from bank_analyzer.domain.imports import RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.csv_importer import read_csv
from bank_analyzer.importer.excel_importer import read_xlsx
from bank_analyzer.importer.ocr_importer import read_pdf_ocr
from bank_analyzer.importer.pdf_importer import read_pdf
from bank_analyzer.security.file_security import StagedFile


def extract_table(
    staged: StagedFile,
    *,
    sheet: str | None = None,
    encoding: str | None = None,
    delimiter: str | None = None,
    header_row: int | None = None,
    max_pdf_pages: int = 100,
    use_ocr: bool = False,
    tesseract_cmd: str | None = None,
) -> RawTable:
    """Extract a validated, staged upload into a RawTable (CSV, XLSX and PDF supported).

    For PDFs, ``use_ocr`` selects the optional OCR path for scans. A scan without ``use_ocr``
    raises NeedsOcrError so the caller can offer it.
    """
    if staged.kind == FileKind.CSV:
        return read_csv(
            staged.path,
            sha256=staged.sha256,
            encoding=encoding,
            delimiter=delimiter,
            header_row=header_row,
        )
    if staged.kind == FileKind.XLSX:
        return read_xlsx(staged.path, sha256=staged.sha256, sheet=sheet, header_row=header_row)
    if staged.kind == FileKind.PDF:
        if use_ocr:
            return read_pdf_ocr(
                staged.path,
                sha256=staged.sha256,
                tesseract_cmd=tesseract_cmd,
                max_pages=max_pdf_pages,
            )
        return read_pdf(staged.path, sha256=staged.sha256, max_pages=max_pdf_pages)
    raise NotImplementedError(f"{staged.kind} import is not implemented")
