"""Deleting all application data, with an honest account of what that does and does not remove.

Each file is overwritten with zeros and then unlinked. This is best effort only. On SSDs and on
journaling or copy-on-write file systems the old bytes may remain elsewhere on the device;
cloud-sync folders, backups you made, OS caches, swap files and the browser may hold copies. Only
full-disk encryption (destroy the key) or physical destruction gives a hard guarantee.

The caller must close its database connection first (an open file cannot be removed on Windows).
A typed confirmation phrase is required so this cannot be triggered by accident.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from bank_analyzer.domain.errors import BankAnalyzerError

CONFIRMATION = "DELETE ALL DATA"
_CHUNK = 1024 * 1024
LIMITS = (
    "Files were overwritten once and deleted. On SSDs and modern file systems old data may "
    "remain on the device.",
    "Copies in cloud-synced folders, in backups you made, in OS caches or swap, and anything you "
    "exported are not removed.",
    "Only full-disk encryption (destroying the key) or physical destruction guarantees removal.",
)


class WipeError(BankAnalyzerError):
    """The wipe was refused or could not finish."""


@dataclass(slots=True)
class WipeReport:
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    limits: tuple[str, ...] = LIMITS


def _overwrite_and_delete(path: Path) -> None:
    size = path.stat().st_size
    with path.open("r+b", buffering=0) as handle:
        remaining = size
        zeros = bytes(min(_CHUNK, max(size, 1)))
        while remaining > 0:
            step = min(len(zeros), remaining)
            handle.write(zeros[:step])
            remaining -= step
        handle.flush()
        with contextlib.suppress(OSError):
            os.fsync(handle.fileno())
    path.unlink()


def database_files(db_path: Path) -> list[Path]:
    """The database, its journal files, and automatic pre-upgrade or pre-restore copies."""
    names = [db_path.name, db_path.name + "-wal", db_path.name + "-shm"]
    found = [db_path.with_name(n) for n in names]
    found += sorted(db_path.parent.glob(db_path.name + ".pre-*.bak"))
    return [p for p in found if p.exists()]


def wipe_data(
    data_dir: Path,
    db_path: Path,
    *,
    confirmation: str,
    include_backups_folder: bool = False,
) -> WipeReport:
    """Remove the database, logs, passcode file and (optionally) the backups folder."""
    if confirmation != CONFIRMATION:
        raise WipeError(f"Type {CONFIRMATION!r} to confirm.")
    targets = database_files(db_path)
    for folder in ("logs", *(("backups",) if include_backups_folder else ())):
        root = data_dir / folder
        if root.is_dir():
            targets += [p for p in sorted(root.rglob("*")) if p.is_file()]
    passcode = data_dir / "passcode.json"
    if passcode.exists():
        targets.append(passcode)
    report = WipeReport()
    for path in targets:
        try:
            _overwrite_and_delete(path)
            report.removed.append(path.name)
        except OSError:
            report.failed.append(path.name)
    for folder in ("logs", "backups"):
        with contextlib.suppress(OSError):
            (data_dir / folder).rmdir()
    return report
