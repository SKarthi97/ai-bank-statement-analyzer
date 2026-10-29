"""Statement extraction. Importers return text-only RawTables and never interpret values."""

from __future__ import annotations

from bank_analyzer.domain.imports import RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.csv_importer import read_csv
from bank_analyzer.importer.excel_importer import read_xlsx
from bank_analyzer.security.file_security import StagedFile


def extract_table(
    staged: StagedFile,
    *,
    sheet: str | None = None,
    encoding: str | None = None,
    delimiter: str | None = None,
    header_row: int | None = None,
) -> RawTable:
    """Extract a validated, staged upload into a RawTable (CSV and XLSX supported)."""
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
    raise NotImplementedError(f"{staged.kind} import is not implemented yet")
