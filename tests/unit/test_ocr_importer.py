from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bank_analyzer.domain.errors import ImportStructureError
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer import ocr_importer
from bank_analyzer.importer.ocr_importer import (
    OcrRow,
    OcrWord,
    build_table,
    find_tesseract,
    parse_tsv,
    read_pdf_ocr,
    run_tesseract,
    words_to_rows,
)
from helpers import FIXTURES

TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def tsv_line(block: int, line: int, left: int, top: int, text: str, conf: float = 95.0) -> str:
    width = 10 * len(text)
    return f"5\t1\t{block}\t1\t{line}\t1\t{left}\t{top}\t{width}\t20\t{conf}\t{text}"


def make_tsv(*lines: str) -> str:
    return "\n".join([TSV_HEADER, *lines]) + "\n"


# One OCR'd statement page: header line, then three data lines. Gaps between columns are wide.
PAGE_TSV = make_tsv(
    "1\t1\t0\t0\t0\t0\t0\t0\t600\t800\t-1\t",  # structural row, must be ignored
    tsv_line(1, 1, 50, 100, "Date"),
    tsv_line(1, 1, 200, 100, "Description"),
    tsv_line(1, 1, 450, 100, "Amount"),
    tsv_line(1, 2, 50, 130, "2024-01-02"),
    tsv_line(1, 2, 200, 130, "SALARY"),
    tsv_line(1, 2, 450, 130, "3000.00", conf=96),
    tsv_line(1, 3, 50, 160, "2024-01-03"),
    tsv_line(1, 3, 200, 160, "GROCERY"),
    tsv_line(1, 3, 280, 160, "MART"),  # same cell as GROCERY: small gap
    tsv_line(1, 3, 450, 160, "-85.40", conf=42),  # a doubtful digit read
)


class TestParseTsv:
    def test_keeps_only_real_words(self) -> None:
        words = parse_tsv(PAGE_TSV)
        assert [w.text for w in words][:4] == ["Date", "Description", "Amount", "2024-01-02"]
        assert all(w.confidence >= 0 for w in words)

    def test_skips_empty_text_negative_confidence_and_malformed_records(self) -> None:
        tsv = make_tsv(
            tsv_line(1, 1, 10, 10, "ok"),
            "5\t1\t1\t1\t1\t1\t10\t10\t10\t20\t-1\tneg",
            "5\t1\t1\t1\t1\t1\t10\t10\t10\t20\t90\t",
            "5\t1\t1\t1\t1\t1\tNaN\t10\t10\t20\t90\tbad",
            "garbage line",
        )
        assert [w.text for w in parse_tsv(tsv)] == ["ok"]

    def test_empty_input(self) -> None:
        assert parse_tsv("") == []
        assert parse_tsv(TSV_HEADER + "\n") == []


class TestWordsToRows:
    def test_lines_in_reading_order_and_cells_split_by_wide_gaps(self) -> None:
        rows = words_to_rows(parse_tsv(PAGE_TSV))
        assert [r.cells for r in rows] == [
            ("Date", "Description", "Amount"),
            ("2024-01-02", "SALARY", "3000.00"),
            ("2024-01-03", "GROCERY MART", "-85.40"),
        ]

    def test_row_confidence_is_the_lowest_word_confidence(self) -> None:
        rows = words_to_rows(parse_tsv(PAGE_TSV))
        assert rows[1].confidence == 95.0 and rows[2].confidence == 42.0

    def test_lines_are_ordered_by_position_not_by_tsv_order(self) -> None:
        tsv = make_tsv(tsv_line(1, 2, 10, 200, "lower"), tsv_line(1, 1, 10, 100, "upper"))
        assert [r.cells for r in words_to_rows(parse_tsv(tsv))] == [("upper",), ("lower",)]

    def test_words_within_a_line_are_ordered_left_to_right(self) -> None:
        words = [
            OcrWord(300, 10, 30, 20, 90, "B", (1, 1, 1)),
            OcrWord(10, 10, 30, 20, 90, "A", (1, 1, 1)),
        ]
        assert words_to_rows(words)[0].cells == ("A", "B")


def page(*rows: tuple[tuple[str, ...], float]) -> list[OcrRow]:
    return [OcrRow(cells=c, confidence=conf) for c, conf in rows]


HEADER = (("Date", "Description", "Amount"), 99.0)


class TestBuildTable:
    def test_basic_table_with_confidence_per_row(self) -> None:
        table = build_table(
            [page(HEADER, (("2024-01-02", "SALARY", "3000.00"), 95.0))], sha256="a" * 64
        )
        assert table.file_kind == FileKind.PDF_OCR
        assert table.headers == ("Date", "Description", "Amount")
        assert table.rows[0].confidence == 95.0 and table.rows[0].location == "page 1"
        assert any("read with OCR" in w for w in table.warnings)

    def test_low_confidence_rows_are_counted_in_warnings(self) -> None:
        table = build_table(
            [
                page(
                    HEADER,
                    (("2024-01-02", "A", "1.00"), 95.0),
                    (("2024-01-03", "B", "2.00"), 79.9),
                    (("2024-01-04", "C", "3.00"), 80.0),
                )
            ],
            sha256="a" * 64,
        )
        assert any("1 row(s) have low OCR confidence" in w for w in table.warnings)

    def test_rows_with_missing_cells_are_left_out_not_shifted(self) -> None:
        """An empty debit cell vanishes in OCR; guessing its column could flip a sign."""
        table = build_table(
            [page(HEADER, (("2024-01-02", "OK", "1.00"), 95.0), (("2024-01-03", "SHORT"), 95.0))],
            sha256="a" * 64,
        )
        assert [r.cells[1] for r in table.rows] == ["OK"]
        assert any("1 line(s)" in w and "left out" in w for w in table.warnings)

    def test_header_repeated_on_later_pages_is_not_data(self) -> None:
        table = build_table(
            [
                page(HEADER, (("2024-01-02", "A", "1.00"), 95.0)),
                page((("2024-01-03", "B", "2.00"), 95.0)),
            ],
            sha256="a" * 64,
        )
        assert [r.location for r in table.rows] == ["page 1", "page 2"]

    def test_text_before_header_is_skipped(self) -> None:
        table = build_table(
            [page((("Acme Bank",), 99.0), HEADER, (("2024-01-02", "A", "1.00"), 95.0))],
            sha256="a" * 64,
        )
        assert len(table.rows) == 1

    def test_no_header_is_an_error(self) -> None:
        with pytest.raises(ImportStructureError, match="OCR could not find"):
            build_table([page((("just", "words"), 90.0))], sha256="a" * 64)

    def test_header_without_usable_rows_is_an_error(self) -> None:
        with pytest.raises(ImportStructureError):
            build_table([page(HEADER)], sha256="a" * 64)


class TestRunner:
    def test_missing_tesseract_is_reported_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ocr_importer, "find_tesseract", lambda command=None: None)
        with pytest.raises(ImportStructureError, match="Tesseract is not installed"):
            read_pdf_ocr(FIXTURES / "image_only.pdf", sha256="0" * 64)

    def test_find_tesseract_with_unknown_command(self) -> None:
        assert find_tesseract("definitely-not-a-real-binary-xyz") is None

    def test_failing_command_is_a_clean_error(self, tmp_path: Path) -> None:
        image = tmp_path / "x.png"
        image.write_bytes(b"not an image")
        # python is not tesseract: it exits non-zero when given these arguments
        with pytest.raises(ImportStructureError, match="OCR failed"):
            run_tesseract(sys.executable, image)

    def test_timeout_is_a_clean_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="tesseract", timeout=1)

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(ImportStructureError, match="too long"):
            run_tesseract("tesseract", tmp_path / "x.png")

    def test_unstartable_command_is_a_clean_error(self, tmp_path: Path) -> None:
        with pytest.raises(ImportStructureError, match="could not be started"):
            run_tesseract(str(tmp_path / "no-such-binary"), tmp_path / "x.png")


class TestReadPdfOcrWithInjectedRunner:
    def test_pages_are_rendered_ocred_and_temp_images_removed(self) -> None:
        seen: list[Path] = []

        def runner(image: Path) -> str:
            assert image.exists() and image.suffix == ".png"  # a real rendered page
            seen.append(image)
            return PAGE_TSV

        table = read_pdf_ocr(FIXTURES / "image_only.pdf", sha256="a" * 64, runner=runner)
        assert len(seen) == 1
        assert not seen[0].exists() and not seen[0].parent.exists()  # private temp dir removed
        assert [r.cells[1] for r in table.rows] == ["SALARY", "GROCERY MART"]
        assert table.rows[1].confidence == 42.0

    def test_temp_dir_removed_when_the_runner_fails(self) -> None:
        seen: list[Path] = []

        def runner(image: Path) -> str:
            seen.append(image)
            raise ImportStructureError("OCR failed on a page.")

        with pytest.raises(ImportStructureError):
            read_pdf_ocr(FIXTURES / "image_only.pdf", sha256="a" * 64, runner=runner)
        assert seen and not seen[0].parent.exists()

    def test_page_limit(self) -> None:
        from bank_analyzer.domain.errors import FileValidationError

        with pytest.raises(FileValidationError):
            read_pdf_ocr(
                FIXTURES / "statement_ruled.pdf",
                sha256="a" * 64,
                runner=lambda image: PAGE_TSV,
                max_pages=1,
            )


@pytest.mark.ocr
@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract is not installed")
def test_live_tesseract_reads_a_rendered_statement() -> None:
    """Needs a real Tesseract: rasterises a text PDF and OCRs it. Skipped when unavailable."""
    table = read_pdf_ocr(FIXTURES / "statement_ruled.pdf", sha256="a" * 64)
    assert table.file_kind == FileKind.PDF_OCR and table.rows
    assert any("3000" in cell for row in table.rows for cell in row.cells)
