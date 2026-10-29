from __future__ import annotations

from pathlib import Path

import pytest

from bank_analyzer.domain.errors import ImportStructureError
from bank_analyzer.importer.csv_importer import decode_text, detect_delimiter, read_csv
from helpers import FIXTURES

SHA = "0" * 64


def write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "s.csv"
    path.write_bytes(content)
    return path


class TestDecoding:
    def test_utf8_bom(self) -> None:
        assert decode_text("﻿Date".encode()) == ("Date", "utf-8")[:1] + ("utf-8-sig",)

    def test_utf16(self) -> None:
        text, enc = decode_text("Date;Betrag\n".encode("utf-16"))
        assert text == "Date;Betrag\n" and enc == "utf-16"

    def test_cp1252_fallback_keeps_accents_and_euro(self) -> None:
        text, enc = decode_text("Café €5".encode("cp1252"))
        assert text == "Café €5" and enc == "cp1252"

    def test_latin1_last_resort(self) -> None:
        text, enc = decode_text(b"abc\x81\x8d")  # bytes undefined in cp1252
        assert enc == "latin-1" and text.startswith("abc")

    def test_explicit_encoding_override(self) -> None:
        assert decode_text("é".encode("latin-1"), "latin-1") == ("é", "latin-1")

    def test_bad_explicit_encoding(self) -> None:
        with pytest.raises(ImportStructureError):
            decode_text(b"\xff\xfe\x00", "ascii")
        with pytest.raises(ImportStructureError):
            decode_text(b"abc", "not-a-codec")


class TestDelimiter:
    @pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
    def test_detects_each(self, delim: str) -> None:
        text = "\n".join(delim.join(r) for r in [["Date", "Desc", "Amount"], ["1", "a", "2"]] * 3)
        assert detect_delimiter(text) == delim

    def test_decimal_commas_do_not_fool_semicolon_detection(self) -> None:
        text = "Datum;Text;Betrag\n01.01.2024;Miete;1.200,50\n02.01.2024;Kaffee;3,20\n"
        assert detect_delimiter(text) == ";"

    def test_single_column_is_not_a_table(self) -> None:
        with pytest.raises(ImportStructureError):
            detect_delimiter("just\nsome\nlines\n")


class TestReadCsv:
    def test_basic_fixture(self) -> None:
        table = read_csv(FIXTURES / "deposit_basic.csv", sha256=SHA)
        assert table.headers == ("Date", "Description", "Amount", "Balance")
        assert len(table.rows) == 9
        assert table.rows[0].row_index == 2  # 1-based record number, header is record 1
        assert table.rows[0].cells[1] == "SALARY ACME CORP"

    def test_preamble_footer_multiline_and_blank_rows(self) -> None:
        table = read_csv(FIXTURES / "bank_preamble_footer.csv", sha256=SHA)
        assert table.headers[:3] == ("Transaction Date", "Details", "Debit")
        # header is on line 5; the quoted two-line cell counts as one record
        descriptions = [r.cells[1] for r in table.rows]
        assert "SUPERMARKET\nBRANCH 12" in descriptions
        assert any("Skipped 4 line(s)" in w for w in table.warnings)
        assert any("1 blank row" in w for w in table.warnings)

    def test_semicolon_european_fixture(self) -> None:
        table = read_csv(FIXTURES / "deposit_eu.csv", sha256=SHA)
        assert table.rows[0].cells == (
            "02.01.2024",
            "GEHALT FIRMA GMBH",
            "",
            "2.500,50",
            "2.500,50",
        )

    def test_cp1252_file_warns_about_encoding(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, "Date,Description,Amount\n2024-01-02,Café Zürich €,-4.50\n".encode("cp1252")
        )
        table = read_csv(path, sha256=SHA)
        assert table.rows[0].cells[1] == "Café Zürich €"
        assert any("cp1252" in w for w in table.warnings)

    def test_utf8_bom_header_not_polluted(self, tmp_path: Path) -> None:
        path = write(tmp_path, "﻿Date,Description,Amount\n2024-01-02,A,1.00\n".encode())
        assert read_csv(path, sha256=SHA).headers[0] == "Date"

    def test_whitespace_trimmed_and_ragged_rows_kept(self, tmp_path: Path) -> None:
        # mostly consistent rows, plus one short and one long row (kept as-is, never padded)
        path = write(
            tmp_path,
            b"Date, Description ,Amount\n"
            b" 2024-01-02 , Shop ,1.00\n"
            b"2024-01-03,A,2.00\n"
            b"2024-01-04,B,3.00\n"
            b"2024-01-05,C,4.00\n"
            b"2024-01-06,Short\n"
            b"2024-01-07,D,5.00,extra\n",
        )
        table = read_csv(path, sha256=SHA)
        assert table.headers == ("Date", "Description", "Amount")
        assert table.rows[0].cells == ("2024-01-02", "Shop", "1.00")
        assert table.rows[4].cells == ("2024-01-06", "Short")
        assert table.rows[5].cells[3] == "extra"

    def test_too_ragged_to_detect_a_delimiter_fails_cleanly(self, tmp_path: Path) -> None:
        path = write(tmp_path, b"Date,Description,Amount\n2024-01-02,Shop\n2024-01-03,A,1.00,x\n")
        with pytest.raises(ImportStructureError, match="delimiter"):
            read_csv(path, sha256=SHA)

    def test_no_header_requires_manual_choice(self, tmp_path: Path) -> None:
        path = write(tmp_path, b"2024-01-02,A,1.00\n2024-01-03,B,2.00\n")
        with pytest.raises(ImportStructureError, match="header"):
            read_csv(path, sha256=SHA)

    def test_manual_header_row_override(self, tmp_path: Path) -> None:
        path = write(tmp_path, b"when,what,how much\n2024-01-02,A,1.00\n")
        table = read_csv(path, sha256=SHA, header_row=1)
        assert table.headers == ("when", "what", "how much")
        assert len(table.rows) == 1

    def test_header_override_out_of_range(self, tmp_path: Path) -> None:
        path = write(tmp_path, b"Date,Description,Amount\n")
        with pytest.raises(ImportStructureError):
            read_csv(path, sha256=SHA, header_row=99)

    def test_explicit_delimiter(self, tmp_path: Path) -> None:
        path = write(tmp_path, b"Date|Description|Amount\n2024-01-02|A, B|1.00\n")
        table = read_csv(path, sha256=SHA, delimiter="|")
        assert table.rows[0].cells[1] == "A, B"

    def test_row_limit_enforced(self, tmp_path: Path) -> None:
        body = "Date,Description,Amount\n" + "2024-01-02,A,1.00\n" * 6
        with pytest.raises(ImportStructureError, match="more than"):
            read_csv(write(tmp_path, body.encode()), sha256=SHA, max_rows=5)

    def test_formula_like_cells_are_inert_text(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, b'Date,Description,Amount\n2024-01-02,"=HYPERLINK(""http://x"")",1.00\n'
        )
        assert read_csv(path, sha256=SHA).rows[0].cells[1] == '=HYPERLINK("http://x")'

    def test_oversized_field_is_a_clean_error(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, b"Date,Description,Amount\n2024-01-02," + b"x" * 200_000 + b",1.00\n"
        )
        with pytest.raises(ImportStructureError):
            read_csv(path, sha256=SHA)
