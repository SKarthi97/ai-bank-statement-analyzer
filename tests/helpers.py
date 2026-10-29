"""Test helpers: build RawTables from synthetic CSV fixtures (the real importer is Phase 3)."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from bank_analyzer.domain.imports import RawRow, RawTable
from bank_analyzer.domain.models import FileKind

FIXTURES = Path(__file__).parent / "fixtures"


def raw_from_csv(name: str, delimiter: str = ",") -> RawTable:
    path = FIXTURES / name
    data = path.read_bytes()
    reader = csv.reader(data.decode("utf-8").splitlines(), delimiter=delimiter)
    records = list(reader)
    return RawTable(
        file_sha256=hashlib.sha256(data).hexdigest(),
        file_kind=FileKind.CSV,
        headers=tuple(records[0]),
        rows=tuple(RawRow(row_index=i, cells=tuple(r)) for i, r in enumerate(records[1:], start=2)),
    )


def raw_from_rows(rows: list[list[str]], headers: tuple[str, ...] = ()) -> RawTable:
    return RawTable(
        file_sha256="0" * 64,
        file_kind=FileKind.CSV,
        headers=headers,
        rows=tuple(RawRow(row_index=i, cells=tuple(r)) for i, r in enumerate(rows, start=2)),
    )


def expected(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
