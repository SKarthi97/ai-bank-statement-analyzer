from __future__ import annotations

from pathlib import Path

import pytest

from bank_analyzer.domain.errors import FileValidationError, ImportStructureError, NeedsOcrError
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.pdf_importer import read_pdf
from bank_analyzer.security.file_security import staged_upload
from helpers import FIXTURES

SHA = "0" * 64


def read(name: str, **kwargs: int) -> object:
    return read_pdf(FIXTURES / name, sha256=SHA, **kwargs)


class TestRuledTables:
    def test_two_page_statement_with_repeated_header(self) -> None:
        table = read_pdf(FIXTURES / "statement_ruled.pdf", sha256=SHA)
        assert table.file_kind == FileKind.PDF
        assert table.headers == ("Date", "Description", "Amount", "Balance")
        assert len(table.rows) == 9  # the page-2 header row must not become a transaction
        assert [r.location for r in table.rows] == ["page 1"] * 5 + ["page 2"] * 4
        assert table.rows[0].cells == ("2024-01-02", "SALARY ACME CORP", "3000.00", "3500.00")
        assert table.rows[5].cells == (
            "2024-02-05",
            "GROCERY MART #1234 CARD 5678",
            "-92.10",
            "2986.51",
        )
        assert table.rows[8].cells[3] == "5906.91"

    def test_row_numbers_are_positions_within_the_page_table(self) -> None:
        table = read_pdf(FIXTURES / "statement_ruled.pdf", sha256=SHA)
        assert [r.row_index for r in table.rows] == [2, 3, 4, 5, 6, 2, 3, 4, 5]

    def test_title_text_above_the_table_is_ignored(self) -> None:
        table = read_pdf(FIXTURES / "statement_ruled.pdf", sha256=SHA)
        assert all("Statement for Checking" not in c for r in table.rows for c in r.cells)

    def test_blank_cells_are_preserved_so_columns_never_shift(self) -> None:
        table = read_pdf(FIXTURES / "statement_debit_credit.pdf", sha256=SHA)
        assert table.headers == ("Transaction Date", "Details", "Debit", "Credit", "Balance")
        by_desc = {r.cells[1]: r.cells for r in table.rows}
        assert by_desc["RENT, MARCH"] == ("2024-03-01", "RENT, MARCH", "900.00", "", "1100.00")
        assert by_desc["PAYMENT RECEIVED"][2:4] == ("", "200.00")  # credit stays in credit

    def test_always_tells_the_user_to_verify_columns(self) -> None:
        table = read_pdf(FIXTURES / "statement_ruled.pdf", sha256=SHA)
        assert any("Check every column" in w for w in table.warnings)


class TestTextAlignedTables:
    def test_fragmenting_layout_is_refused_not_truncated(self) -> None:
        """Multi-word descriptions get split mid-word by the text detector: never use that."""
        with pytest.raises(ImportStructureError, match="cannot be read reliably"):
            read_pdf(FIXTURES / "statement_borderless.pdf", sha256=SHA)

    def test_cleanly_separable_layout_is_read_with_a_warning(self) -> None:
        table = read_pdf(FIXTURES / "statement_borderless_simple.pdf", sha256=SHA)
        assert table.headers == ("Date", "Description", "Amount", "Balance")
        assert [r.cells for r in table.rows] == [
            ("2024-01-02", "SALARY", "3000.00", "3500.00"),
            ("2024-01-03", "GROCERY", "-85.40", "3414.60"),
            ("2024-01-15", "POWER", "-120.00", "3294.60"),
        ]
        assert any("text alignment" in w for w in table.warnings)


class TestUnreadableInputs:
    def test_scan_without_text_layer_asks_for_ocr(self) -> None:
        with pytest.raises(NeedsOcrError, match="OCR"):
            read_pdf(FIXTURES / "image_only.pdf", sha256=SHA)

    def test_needs_ocr_is_an_import_structure_error(self) -> None:
        assert issubclass(NeedsOcrError, ImportStructureError)

    def test_encrypted_pdf_gets_a_clear_message(self) -> None:
        with pytest.raises(ImportStructureError, match="password-protected"):
            read_pdf(FIXTURES / "encrypted.pdf", sha256=SHA)

    def test_garbage_after_a_valid_signature(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.pdf"
        path.write_bytes(b"%PDF-1.7\nthis is not really a pdf\n%%EOF")
        with pytest.raises(ImportStructureError):
            read_pdf(path, sha256=SHA)

    def test_truncated_pdf(self, tmp_path: Path) -> None:
        data = (FIXTURES / "statement_ruled.pdf").read_bytes()
        path = tmp_path / "cut.pdf"
        path.write_bytes(data[: len(data) // 3])
        with pytest.raises(ImportStructureError):
            read_pdf(path, sha256=SHA)

    def test_text_without_any_table(self, tmp_path: Path) -> None:
        from reportlab.pdfgen import canvas

        path = tmp_path / "prose.pdf"
        c = canvas.Canvas(str(path))
        for i in range(10):
            c.drawString(50, 800 - 20 * i, "This is a letter and not a bank statement at all.")
        c.save()
        with pytest.raises(ImportStructureError, match="No transaction table") as info:
            read_pdf(path, sha256=SHA)
        assert not isinstance(info.value, NeedsOcrError)  # it has text, so OCR would not help

    def test_page_limit(self) -> None:
        with pytest.raises(FileValidationError, match="more than 1 pages"):
            read_pdf(FIXTURES / "statement_ruled.pdf", sha256=SHA, max_pages=1)


class TestActiveContent:
    def test_javascript_is_reported_at_staging_and_never_run(self) -> None:
        with staged_upload(FIXTURES / "active_content.pdf") as staged:
            assert staged.kind == FileKind.PDF
            assert any("JavaScript" in w for w in staged.warnings)

    def test_clean_pdfs_have_no_active_content_warning(self) -> None:
        with staged_upload(FIXTURES / "statement_ruled.pdf") as staged:
            assert staged.warnings == ()
