"""Check that the application is configured and built to stay on this computer.

    python scripts/verify_offline.py            # static scan + configuration checks
    python scripts/verify_offline.py --static   # only the scan for remote references
    python scripts/verify_offline.py --config   # only the configuration checks

Exit code 0 means every check passed; 1 means at least one problem was found.

A pass means no remote reference exists in the application's own files and the local-only settings
are in place. It does NOT prove the app never connects anywhere: dependencies, Ollama, Tesseract and
the browser are outside this check. For that, follow the dynamic procedure and firewall guidance in
docs/04_SECURITY_AND_THREAT_MODEL.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bank_analyzer.config import load_settings  # noqa: E402
from bank_analyzer.domain.errors import ConfigError  # noqa: E402
from bank_analyzer.security.offline_check import check_config, scan_static  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--static", action="store_true", help="only scan for remote references")
    parser.add_argument("--config", action="store_true", help="only check configuration")
    args = parser.parse_args(argv)
    run_static = args.static or not args.config
    run_config = args.config or not args.static

    problems = 0
    if run_static:
        findings = scan_static(ROOT)
        print(f"Static scan: {len(findings)} remote reference(s) found.")
        for finding in findings:
            print("  " + str(finding))
        problems += len(findings)
    if run_config:
        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"Configuration: {exc}")
            return 1
        findings = check_config(ROOT, settings)
        print(f"Configuration: {len(findings)} problem(s) found.")
        for finding in findings:
            print("  " + str(finding))
        problems += len(findings)
    print("RESULT:", "PASS" if problems == 0 else "FAIL")
    print(
        "Note: this does not prove the app never connects anywhere. See docs/04 section 6 for the "
        "dynamic check and firewall guidance."
    )
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
