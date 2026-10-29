from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from bank_analyzer.domain.errors import FileValidationError
from bank_analyzer.domain.models import FileKind
from bank_analyzer.security.file_security import (
    kind_for_extension,
    safe_display_name,
    staged_upload,
)
from helpers import FIXTURES


def write_zip(path: Path, entries: dict[str, bytes], *, deflate: bool = True) -> Path:
    mode = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", mode) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


VALID_PARTS = {"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>"}


class TestExtensions:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [("a.csv", FileKind.CSV), ("A.CSV", FileKind.CSV), ("s.xlsx", FileKind.XLSX)],
    )
    def test_allowed(self, name: str, kind: FileKind) -> None:
        assert kind_for_extension(name) == kind

    @pytest.mark.parametrize(
        "name",
        [
            "a.exe",
            "a.xls",
            "a.xlsm",
            "a.pdf",
            "a.csv.exe",
            "a.csv ",
            "noext",
            "",
            "a.xlsx.zip",
            "..\\..\\a.sh",
        ],
    )
    def test_rejected(self, name: str) -> None:
        with pytest.raises(FileValidationError):
            kind_for_extension(name)


class TestDisplayName:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\a\\statement.csv", "statement.csv"),
            ("a\x00b\nc.csv", "a b c.csv"),
            ("   ", "statement"),
            ("", "statement"),
        ],
    )
    def test_sanitised(self, raw: str, clean: str) -> None:
        assert safe_display_name(raw) == clean

    def test_length_capped(self) -> None:
        assert len(safe_display_name("x" * 500 + ".csv")) == 100


class TestStaging:
    def test_valid_csv_staged_hashed_and_cleaned_up(self, tmp_path: Path) -> None:
        source = tmp_path / "my statement.csv"
        source.write_bytes(b"Date,Amount\n2024-01-01,1.00\n")
        with staged_upload(source) as staged:
            workdir = staged.path.parent
            assert staged.path.exists() and staged.path != source
            assert staged.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
            assert staged.kind == FileKind.CSV
            assert staged.display_name == "my statement.csv"
        assert not workdir.exists()

    def test_cleanup_happens_when_body_raises(self, tmp_path: Path) -> None:
        source = tmp_path / "s.csv"
        source.write_bytes(b"a,b\n1,2\n")
        workdir: Path | None = None
        with pytest.raises(RuntimeError), staged_upload(source) as staged:
            workdir = staged.path.parent
            raise RuntimeError("boom")
        assert workdir is not None and not workdir.exists()

    def test_original_is_never_modified(self, tmp_path: Path) -> None:
        source = tmp_path / "s.csv"
        source.write_bytes(b"a,b\n1,2\n")
        before = (source.read_bytes(), source.stat().st_mtime_ns)
        with staged_upload(source) as staged:
            staged.path.write_bytes(b"tampered")  # even writing to the copy must not touch it
        assert (source.read_bytes(), source.stat().st_mtime_ns) == before

    def test_staged_name_does_not_use_user_name(self, tmp_path: Path) -> None:
        source = tmp_path / "evil name; rm -rf.csv"
        source.write_bytes(b"a,b\n1,2\n")
        with staged_upload(source) as staged:
            assert staged.path.name == "upload.csv"

    def test_original_name_override_for_uploads(self, tmp_path: Path) -> None:
        raw = tmp_path / "tmpabc123"
        raw.write_bytes(b"a,b\n1,2\n")
        with staged_upload(raw, original_name="bank.csv") as staged:
            assert staged.display_name == "bank.csv"

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "e.csv"
        source.write_bytes(b"")
        with pytest.raises(FileValidationError), staged_upload(source):
            pass

    def test_oversize_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "big.csv"
        source.write_bytes(b"a,b\n" + b"1,2\n" * 300_000)  # ~1.2 MB
        with pytest.raises(FileValidationError), staged_upload(source, max_mb_tabular=1):
            pass

    def test_missing_and_directory_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(FileValidationError), staged_upload(tmp_path / "nope.csv"):
            pass
        folder = tmp_path / "dir.csv"
        folder.mkdir()
        with pytest.raises(FileValidationError), staged_upload(folder):
            pass

    @pytest.mark.parametrize(
        "content",
        [b"a,b\x00c\n1,2\n", b"%PDF-1.7\n...", b"MZ\x90\x00", b"PK\x03\x04junk", b"\x7fELFxx"],
    )
    def test_binary_masquerading_as_csv_rejected(self, tmp_path: Path, content: bytes) -> None:
        source = tmp_path / "x.csv"
        source.write_bytes(content)
        with pytest.raises(FileValidationError), staged_upload(source):
            pass

    def test_utf16_csv_is_accepted(self, tmp_path: Path) -> None:
        source = tmp_path / "u16.csv"
        source.write_bytes("Date,Amount\n".encode("utf-16"))
        with staged_upload(source) as staged:
            assert staged.kind == FileKind.CSV

    def test_failed_validation_leaves_no_temp_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        created: list[str] = []
        real = tempfile.mkdtemp

        def spy(*args: object, **kwargs: object) -> str:
            path = real(*args, **kwargs)  # type: ignore[arg-type]
            created.append(path)
            return path

        monkeypatch.setattr(tempfile, "mkdtemp", spy)
        source = tmp_path / "x.csv"
        source.write_bytes(b"\x00\x01\x02")
        with pytest.raises(FileValidationError), staged_upload(source):
            pass
        assert created and not Path(created[0]).exists()


class TestXlsxContainer:
    def test_real_fixture_accepted(self) -> None:
        with staged_upload(FIXTURES / "bank_basic.xlsx") as staged:
            assert staged.kind == FileKind.XLSX

    def test_not_a_zip_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "fake.xlsx"
        source.write_bytes(b"this is plain text, not a workbook")
        with pytest.raises(FileValidationError), staged_upload(source):
            pass

    def test_zip_without_workbook_parts_rejected(self, tmp_path: Path) -> None:
        source = write_zip(tmp_path / "notbook.xlsx", {"readme.txt": b"hi"})
        with pytest.raises(FileValidationError), staged_upload(source):
            pass

    def test_zip_bomb_rejected(self, tmp_path: Path) -> None:
        source = write_zip(
            tmp_path / "bomb.xlsx", {**VALID_PARTS, "xl/worksheets/sheet1.xml": b"\x00" * 5_000_000}
        )
        assert source.stat().st_size < 100_000  # tiny on disk, 5 MB expanded
        with pytest.raises(FileValidationError, match="compressed"), staged_upload(source):
            pass

    def test_macro_workbook_rejected(self, tmp_path: Path) -> None:
        source = write_zip(tmp_path / "macro.xlsx", {**VALID_PARTS, "xl/vbaProject.bin": b"x"})
        with pytest.raises(FileValidationError, match="macros"), staged_upload(source):
            pass

    def test_unsafe_internal_paths_rejected(self, tmp_path: Path) -> None:
        source = write_zip(tmp_path / "slip.xlsx", {**VALID_PARTS, "../evil.txt": b"x"})
        with pytest.raises(FileValidationError, match="unsafe"), staged_upload(source):
            pass

    def test_too_many_entries_rejected(self, tmp_path: Path) -> None:
        entries = {**VALID_PARTS, **{f"x/{i}.xml": b"a" for i in range(1100)}}
        source = write_zip(tmp_path / "many.xlsx", entries)
        with pytest.raises(FileValidationError, match="too many"), staged_upload(source):
            pass

    def test_truncated_zip_rejected(self, tmp_path: Path) -> None:
        good = (FIXTURES / "bank_basic.xlsx").read_bytes()
        source = tmp_path / "cut.xlsx"
        source.write_bytes(good[: len(good) // 2])
        with pytest.raises(FileValidationError), staged_upload(source):
            pass

    def test_messages_do_not_echo_file_content(self, tmp_path: Path) -> None:
        source = tmp_path / "secret-account-12345678.xlsx"
        source.write_bytes(b"IBAN DE89370400440532013000")
        with pytest.raises(FileValidationError) as info, staged_upload(source):
            pass
        assert "12345678" not in str(info.value) and "DE89" not in str(info.value)
