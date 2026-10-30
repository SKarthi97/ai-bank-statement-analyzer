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


def write_shaded_statement(
    path: Path, headings: tuple[str, ...], password: str | None = None
) -> None:
    """A text PDF in the layout of a common bank statement: shaded rows, no ruled lines, no year."""
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    rows = [
        ("28/10", "BALANCE B/F", "", "", "100,000.00"),
        ("31/10", "INTEREST PAY SYS-GEN", "", "1,500.00", "101,500.00"),
        ("02/11", "ATM WITHDRAWAL TOWN BR.", "2,500.00", "", "99,000.00"),
        ("02/11", "ATM SERVICE CHARGE", "5.00", "", "98,995.00"),
        ("30/11", "BALANCE C/F", "", "", "98,995.00"),
    ]
    c = canvas.Canvas(str(path), encrypt=password)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(200, 810, "STATEMENT OF ACCOUNT   DATE: 30/11/2022")
    c.drawString(40, 795, "CURRENCY : LKR")
    c.setFillColor(colors.HexColor("#FFD700"))
    c.rect(40, 740, 520, 16, fill=1, stroke=0)
    c.setFillColor(colors.black)
    for text, x, right in zip(headings, (45, 110, 390, 470, 550), (0, 0, 1, 1, 1), strict=True):
        (c.drawRightString if right else c.drawString)(x, 745, text)
    c.setFont("Helvetica", 9)
    y = 725
    for i, (d, p, pay, rec, bal) in enumerate(rows):
        c.setFillColor(colors.HexColor("#F2EFC0" if i % 2 == 0 else "#FBF9E0"))
        c.rect(40, y - 4, 520, 16, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.drawString(45, y, d)
        c.drawString(110, y, p)
        c.drawRightString(390, y, pay)
        c.drawRightString(470, y, rec)
        c.drawRightString(550, y, bal)
        y -= 18
    c.save()
