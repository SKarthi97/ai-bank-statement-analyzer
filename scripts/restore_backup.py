"""Restore an encrypted backup into the application database. Close the app first.

    python scripts/restore_backup.py <backup file> [--overwrite] [--database <path>]

The passphrase is asked for on the terminal and is never printed or stored. If a database already
exists, ``--overwrite`` is required, and a copy of the old one is kept next to it
(``*.pre-restore.bak``).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bank_analyzer.config import load_settings  # noqa: E402
from bank_analyzer.domain.errors import BankAnalyzerError  # noqa: E402
from bank_analyzer.services.backup import restore_backup, verify_backup  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument(
        "--database", type=Path, help="target database (default: the application database)"
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing database")
    parser.add_argument(
        "--verify-only", action="store_true", help="check the backup without restoring"
    )
    args = parser.parse_args(argv)
    try:
        target = args.database or load_settings().db_path
        passphrase = getpass.getpass("Backup passphrase: ")
        if args.verify_only:
            info = verify_backup(args.backup, passphrase)
            print(
                f"OK: decrypts and passes its integrity check (schema version {info.schema_version})."
            )
            return 0
        info = restore_backup(args.backup, passphrase, target, overwrite=args.overwrite)
    except BankAnalyzerError as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"Restored to {info.path} (schema version {info.schema_version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
