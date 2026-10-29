"""File-system protections and upload validation.

Uploads are validated (extension allowlist, size, content signature, archive limits), then
copied to a private temporary directory and hashed. Importers only ever read that staged copy,
so the original file is never modified. The user's file name is treated as untrusted display
text: it is sanitised for display and never used to build a path.

On POSIX the directories are chmod 0700 and files 0600. On Windows, ``chmod`` does not set ACLs;
the default per-user locations (%LOCALAPPDATA%, the user temp folder) are inherited as user-only,
and a stricter ACL check is part of Phase 7. This limitation is intentional and documented.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath

from bank_analyzer.domain.errors import FileValidationError
from bank_analyzer.domain.models import FileKind

_MB = 1024 * 1024
_ZIP_MAGIC = b"PK\x03\x04"
_CHUNK = 1024 * 1024

# Archive limits for XLSX (a zip container). Real statements are far below these.
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * _MB
MAX_ZIP_RATIO = 100  # per-entry uncompressed/compressed ratio

_EXTENSIONS = {".csv": FileKind.CSV, ".xlsx": FileKind.XLSX}


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the current user where supported."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(stat.S_IRWXU)
    return path


def restrict_file(path: Path) -> None:
    """Restrict an existing file to the current user where supported."""
    if os.name == "posix" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def safe_display_name(name: str, max_length: int = 100) -> str:
    """A sanitised base name for display only. Never use the result as a path."""
    base = PurePath(name.replace("\\", "/")).name
    base = "".join(" " if unicodedata.category(c) in {"Cc", "Cf"} else c for c in base)
    base = re.sub(r"\s+", " ", base).strip()
    return base[:max_length] or "statement"


def kind_for_extension(name: str) -> FileKind:
    """Map the final extension to a supported FileKind, or raise FileValidationError.

    Only the last suffix counts, so ``statement.csv.exe`` is rejected. Macro-enabled and legacy
    Excel formats are refused on purpose.
    """
    suffix = PurePath(name.replace("\\", "/")).suffix.lower()
    kind = _EXTENSIONS.get(suffix)
    if kind is None:
        raise FileValidationError("Unsupported file type. Use .csv or .xlsx.")
    return kind


def _check_xlsx_container(path: Path) -> None:
    """Reject non-zip files, zip bombs and macro-enabled workbooks."""
    if not zipfile.is_zipfile(path):
        raise FileValidationError("The file is not a valid .xlsx workbook.")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise FileValidationError("The workbook contains too many internal files.")
            names = {i.filename for i in infos}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise FileValidationError("The file is not a valid .xlsx workbook.")
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise FileValidationError("Workbooks containing macros are not accepted.")
            total = 0
            for info in infos:
                if info.filename.startswith(("/", "\\")) or ".." in PurePath(info.filename).parts:
                    raise FileValidationError("The workbook contains unsafe internal paths.")
                total += info.file_size
                if (
                    info.compress_size > 0
                    and info.file_size > _MB
                    and info.file_size / info.compress_size > MAX_ZIP_RATIO
                ):
                    raise FileValidationError("The workbook is suspiciously compressed.")
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise FileValidationError("The workbook expands to an unreasonable size.")
    except zipfile.BadZipFile as exc:
        raise FileValidationError("The file is not a valid .xlsx workbook.") from exc


def _check_csv_content(path: Path) -> None:
    """Reject binary content masquerading as CSV (NUL bytes outside UTF-16/32 text)."""
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return  # UTF-16 text legitimately contains NUL bytes
    if b"\x00" in head or head.startswith((_ZIP_MAGIC, b"%PDF", b"MZ", b"\x7fELF")):
        raise FileValidationError("The file does not look like a text CSV file.")


@dataclass(frozen=True, slots=True)
class StagedFile:
    """A validated private copy of an upload. ``path`` is safe to read; never write to it."""

    path: Path
    kind: FileKind
    sha256: str
    size_bytes: int
    display_name: str


@contextmanager
def staged_upload(
    source: Path,
    *,
    original_name: str | None = None,
    max_mb_tabular: int = 25,
) -> Iterator[StagedFile]:
    """Validate ``source``, copy it into a private temp dir, hash it, and clean up on exit.

    ``original_name`` is the user-visible name (for uploads whose temp name differs). The
    private directory is removed even if the body raises.
    """
    name = original_name or source.name
    kind = kind_for_extension(name)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise FileValidationError("The file could not be read.") from exc
    if not source.is_file():
        raise FileValidationError("The selected path is not a file.")
    if size == 0:
        raise FileValidationError("The file is empty.")
    if size > max_mb_tabular * _MB:
        raise FileValidationError(f"The file is larger than the {max_mb_tabular} MB limit.")

    workdir = Path(tempfile.mkdtemp(prefix="bankan-"))
    try:
        if os.name == "posix":
            workdir.chmod(stat.S_IRWXU)
        # The staged name is generated, never derived from the user's file name.
        staged = workdir / f"upload{'.xlsx' if kind == FileKind.XLSX else '.csv'}"
        digest = hashlib.sha256()
        with source.open("rb") as src, staged.open("wb") as dst:
            while chunk := src.read(_CHUNK):
                digest.update(chunk)
                dst.write(chunk)
        restrict_file(staged)
        if kind == FileKind.XLSX:
            _check_xlsx_container(staged)
        else:
            _check_csv_content(staged)
        yield StagedFile(
            path=staged,
            kind=kind,
            sha256=digest.hexdigest(),
            size_bytes=size,
            display_name=safe_display_name(name),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
