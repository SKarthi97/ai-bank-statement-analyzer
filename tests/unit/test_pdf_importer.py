from __future__ import annotations

from pathlib import Path

import pytest

from bank_analyzer.domain.errors import (
    FileValidationError,
    ImportStructureError,
    NeedsOcrError,
    PasswordRequiredError,
    WrongPasswordError,
)
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer.pdf_importer import read_pdf
from bank_analyzer.security.file_security import staged_upload
from helpers import FIXTURES, write_shaded_statement

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
    def test_multiword_descriptions_are_read_whole(self) -> None:
        """pdfplumber's own text detector splits these mid-word; the word-position one does not."""
        table = read_pdf(FIXTURES / "statement_borderless.pdf", sha256=SHA)
        assert table.headers == ("Date", "Description", "Amount", "Balance")
        assert len(table.rows) == 9
        assert table.rows[1].cells == (
            "2024-01-03",
            "GROCERY MART #1234 CARD 5678",
            "-85.40",
            "3414.60",
        )
        assert table.rows[-1].cells == ("2024-02-29", "SALARY ACME CORP", "3000.00", "5906.91")

    def test_fragmenting_layout_is_refused_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If only pdfplumber's text detector is left and it splits mid-word, never use it."""
        monkeypatch.setattr("bank_analyzer.importer.pdf_importer._words_table", lambda page: None)
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


class TestStatementsWithoutAYear:
    def test_dates_without_a_year_are_recognised_as_a_statement_table(self, tmp_path: Path) -> None:
        path = tmp_path / "shaded.pdf"
        write_shaded_statement(path, ("DATE", "PARTICULARS", "PAYMENTS", "RECEIPTS", "BALANCE"))
        table = read_pdf(path, sha256=SHA)
        assert table.headers == ("DATE", "PARTICULARS", "PAYMENTS", "RECEIPTS", "BALANCE")
        assert len(table.rows) == 5
        assert table.rows[2].cells == (
            "02/11",
            "ATM WITHDRAWAL TOWN BR.",
            "2,500.00",
            "",
            "99,000.00",
        )
        assert "30/11/2022" in table.context_text  # the year lives in the heading text

    def test_unfamiliar_headings_are_used_with_a_warning(self, tmp_path: Path) -> None:
        path = tmp_path / "odd.pdf"
        write_shaded_statement(path, ("Dt", "Narr", "Out", "In", "Bal"))
        table = read_pdf(path, sha256=SHA)
        assert table.headers == ("Dt", "Narr", "Out", "In", "Bal")
        assert len(table.rows) == 5
        assert any("headings were not ones this app knows" in w for w in table.warnings)


def _positioned(path: Path, lines: list[list[tuple[float, str]]]) -> None:
    """Draw each line's (x, text) cells top to bottom with a plain font."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.setFont("Helvetica", 9)
    y = 780
    for cells in lines:
        for x, text in cells:
            c.drawString(x, y, text)
        y -= 16
    c.save()


class TestWordPositionExtractor:
    HEAD = [(40, "Date"), (110, "Details"), (350, "Out"), (430, "In")]

    def test_a_wrapped_description_becomes_its_own_row_and_the_footer_is_left_out(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "wrapped.pdf"
        _positioned(
            path,
            [
                [(40, "STATEMENT HEADING")],
                self.HEAD,
                [(40, "02/11"), (110, "CARD PURCHASE SOME SHOP"), (350, "25.00")],
                [(110, "LONDON GB")],
                [(40, "03/11"), (110, "SALARY"), (430, "900.00")],
                [(40, "END OF STATEMENT THANK YOU")],
            ],
        )
        table = read_pdf(path, sha256=SHA)
        assert [r.cells for r in table.rows] == [
            ("02/11", "CARD PURCHASE SOME SHOP", "25.00", ""),
            ("", "LONDON GB", "", ""),
            ("03/11", "SALARY", "", "900.00"),
        ]

    def test_a_heading_that_spans_two_columns_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "merged.pdf"
        _positioned(
            path,
            [
                [(40, "Date"), (110, "Details"), (350, "Out"), (460, "In")],
                [(40, "02/11"), (110, "A"), (350, "25.00")],
                [(40, "03/11"), (110, "B"), (350, "1.00"), (460, "2.00")],
                [
                    (40, "04/11"),
                    (110, "LONG TEXT THAT RUNS INTO THE NEXT COLUMN OVER THERE"),
                    (460, "3.00"),
                ],
            ],
        )
        # The long description closes the gutter between Details and Out, so the columns cannot
        # be told apart reliably: the extractor must not guess.
        import pdfplumber

        from bank_analyzer.importer import pdf_importer

        with pdfplumber.open(path) as pdf:
            assert pdf_importer._words_table(pdf.pages[0]) is None

    def test_a_nameless_column_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "nameless.pdf"
        _positioned(
            path,
            [
                [(40, "Date"), (110, "Details"), (430, "In")],
                [(40, "02/11"), (110, "A"), (350, "25.00"), (430, "5.00")],
                [(40, "03/11"), (110, "B"), (350, "1.00"), (430, "2.00")],
            ],
        )
        import pdfplumber

        from bank_analyzer.importer import pdf_importer

        with pdfplumber.open(path) as pdf:
            assert pdf_importer._words_table(pdf.pages[0]) is None


class TestPasswordProtectedPdf:
    HEADINGS = ("DATE", "PARTICULARS", "PAYMENTS", "RECEIPTS", "BALANCE")

    def _locked(self, tmp_path: Path) -> Path:
        path = tmp_path / "locked.pdf"
        write_shaded_statement(path, self.HEADINGS, password="s3cret-pw")
        return path

    def test_the_right_password_opens_it(self, tmp_path: Path) -> None:
        table = read_pdf(self._locked(tmp_path), sha256=SHA, password="s3cret-pw")
        assert table.headers == self.HEADINGS
        assert len(table.rows) == 5
        assert table.rows[1].cells[3] == "1,500.00"

    def test_no_password_asks_for_one(self, tmp_path: Path) -> None:
        with pytest.raises(PasswordRequiredError, match="password-protected"):
            read_pdf(self._locked(tmp_path), sha256=SHA)

    def test_an_empty_password_counts_as_none(self, tmp_path: Path) -> None:
        with pytest.raises(PasswordRequiredError):
            read_pdf(self._locked(tmp_path), sha256=SHA, password="")

    def test_a_wrong_password_is_a_different_error_and_never_echoed(self, tmp_path: Path) -> None:
        with pytest.raises(WrongPasswordError) as info:
            read_pdf(self._locked(tmp_path), sha256=SHA, password="not-the-one")
        assert "not-the-one" not in str(info.value)
        assert "s3cret-pw" not in str(info.value)

    def test_both_are_import_structure_errors_so_old_handlers_still_catch_them(
        self, tmp_path: Path
    ) -> None:
        assert issubclass(PasswordRequiredError, ImportStructureError)
        assert issubclass(WrongPasswordError, ImportStructureError)

    def test_the_ocr_path_also_asks_for_the_password(self, tmp_path: Path) -> None:
        from bank_analyzer.importer.ocr_importer import read_pdf_ocr

        with pytest.raises(PasswordRequiredError):
            read_pdf_ocr(self._locked(tmp_path), sha256=SHA, runner=lambda image: "")

    def test_staging_says_the_script_check_could_not_look_inside(self) -> None:
        with staged_upload(FIXTURES / "encrypted.pdf") as staged:
            assert any("encrypted" in w for w in staged.warnings)
