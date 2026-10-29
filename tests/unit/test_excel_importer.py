from __future__ import annotations

import math
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bank_analyzer.domain.errors import ImportStructureError
from bank_analyzer.importer.excel_importer import _Counters, _to_text, list_sheets, read_xlsx
from helpers import FIXTURES

SHA = "0" * 64


class TestCellConversion:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (None, ""),
            ("  hello   world ", "hello world"),
            (5, "5"),
            (True, "TRUE"),
            (85.4, "85.4"),
            (-0.07, "-0.07"),
            (1234.56, "1234.56"),
            (1e-7, "0.0000001"),
            (1e21, "1000000000000000000000"),
            (Decimal("12.30"), "12.30"),
            (date(2024, 1, 5), "2024-01-05"),
            (datetime(2024, 1, 5, 0, 0), "2024-01-05"),
        ],
    )
    def test_conversion(self, value: object, text: str) -> None:
        assert _to_text(value, _Counters()) == text

    def test_float_noise_is_preserved_not_rounded(self) -> None:
        assert _to_text(0.1 + 0.2, _Counters()) == "0.30000000000000004"

    def test_time_part_counted(self) -> None:
        counters = _Counters()
        assert _to_text(datetime(2024, 1, 5, 14, 30), counters) == "2024-01-05"
        assert counters.datetimes_with_time == 1

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_non_finite_dropped_and_counted(self, value: float) -> None:
        counters = _Counters()
        assert _to_text(value, counters) == ""
        assert counters.non_finite == 1


class TestReadXlsx:
    def test_basic_fixture(self) -> None:
        table = read_xlsx(FIXTURES / "bank_basic.xlsx", sha256=SHA)
        assert table.headers == ("Date", "Description", "Amount", "Balance")
        assert len(table.rows) == 10  # 9 transactions + the closing-balance footer
        first = table.rows[0]
        assert first.cells == ("2024-01-02", "SALARY ACME CORP", "3000", "3500")
        assert first.row_index == 5 and first.location == "Transactions"
        assert table.rows[1].cells[2] == "-85.4"
        assert any("Skipped 3 row" in w for w in table.warnings)

    def test_multi_sheet_skips_hidden_and_headerless_sheets(self) -> None:
        assert list_sheets(FIXTURES / "multi_sheet.xlsx") == ["Summary", "Transactions"]
        table = read_xlsx(FIXTURES / "multi_sheet.xlsx", sha256=SHA)
        assert [r.cells[1] for r in table.rows] == ["SALARY", "COFFEE"]
        assert any("2 visible sheets" in w for w in table.warnings)

    def test_explicit_sheet_choice(self) -> None:
        table = read_xlsx(FIXTURES / "multi_sheet.xlsx", sha256=SHA, sheet="Transactions")
        assert len(table.rows) == 2

    def test_explicit_headerless_sheet_fails_cleanly(self) -> None:
        with pytest.raises(ImportStructureError, match="header"):
            read_xlsx(FIXTURES / "multi_sheet.xlsx", sha256=SHA, sheet="Summary")

    def test_hidden_or_missing_sheet_rejected(self) -> None:
        for name in ("Hidden", "Nope"):
            with pytest.raises(ImportStructureError, match="does not exist or is hidden"):
                read_xlsx(FIXTURES / "multi_sheet.xlsx", sha256=SHA, sheet=name)

    def test_text_amounts_preserved_verbatim(self) -> None:
        table = read_xlsx(FIXTURES / "text_amounts_eu.xlsx", sha256=SHA)
        assert table.rows[0].cells == (
            "02.01.2024",
            "GEHALT FIRMA GMBH",
            "",
            "2.500,50",
            "2.500,50",
        )

    def test_datetime_time_part_warned(self) -> None:
        table = read_xlsx(FIXTURES / "datetime_with_time.xlsx", sha256=SHA)
        assert table.rows[0].cells[0] == "2024-01-02"
        assert any("time part" in w for w in table.warnings)

    def test_float_noise_reaches_parser_unrounded(self) -> None:
        table = read_xlsx(FIXTURES / "float_noise.xlsx", sha256=SHA)
        assert table.rows[1].cells[2] == "0.30000000000000004"

    def test_manual_header_row(self) -> None:
        table = read_xlsx(FIXTURES / "bank_basic.xlsx", sha256=SHA, header_row=4)
        assert table.headers[0] == "Date"

    def test_row_limit(self) -> None:
        with pytest.raises(ImportStructureError, match="more than"):
            read_xlsx(FIXTURES / "bank_basic.xlsx", sha256=SHA, max_rows=3)

    def test_garbage_inside_valid_zip_is_a_clean_error(self, tmp_path: Path) -> None:
        path = tmp_path / "garbage.xlsx"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", b"not xml at all")
            z.writestr("xl/workbook.xml", b"<broken")
        with pytest.raises(ImportStructureError):
            read_xlsx(path, sha256=SHA)

    def test_text_file_is_a_clean_error(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.xlsx"
        path.write_text("not a workbook")
        with pytest.raises(ImportStructureError):
            read_xlsx(path, sha256=SHA)
