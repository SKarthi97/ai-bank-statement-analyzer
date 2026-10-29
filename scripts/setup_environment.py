"""First-time setup: create the private data folder and the database, then check them.

    python scripts/setup_environment.py

Safe to run again: an existing database is only upgraded (after an automatic backup), never
replaced. Prints where your data will live and any warning (for example a cloud-synced folder or a
folder other accounts can read). Exit code 0 means the environment is ready.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bank_analyzer.config import load_settings, sync_folder_warning  # noqa: E402
from bank_analyzer.deploy import setup_environment  # noqa: E402
from bank_analyzer.domain.errors import BankAnalyzerError  # noqa: E402


def main() -> int:
    try:
        settings = load_settings()
        report = setup_environment(settings)
    except BankAnalyzerError as exc:
        print(f"Setup failed: {exc}")
        return 1
    print(f"Data folder : {report.data_dir}")
    print(f"Database    : {report.db_path} (schema version {report.schema_version})")
    warnings = list(report.warnings)
    sync = sync_folder_warning(report.data_dir)
    if sync:
        warnings.insert(0, sync)
    for warning in warnings:
        print(f"WARNING: {warning}")
    print("Ready." if report.healthy else "The database did not pass its health check.")
    print("Next: python scripts/check_model.py   then   python scripts/run_app.py")
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
