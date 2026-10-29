"""Prepare, and verify, an offline installation bundle.

On an ONLINE preparation machine (same operating system, CPU and Python version as the target):

    python scripts/make_offline_bundle.py build <empty folder>

This copies the application, downloads every pinned package as a wheel into ``wheelhouse/``, and
writes ``SHA256SUMS``. Copy the folder to removable media together with the Ollama installer, the
model files and (optionally) Tesseract; this script does not fetch those.

On the OFFLINE target machine:

    python scripts/make_offline_bundle.py verify <bundle folder>
    python scripts/make_offline_bundle.py install-command <bundle folder>

``verify`` checks every file against SHA256SUMS. ``install-command`` prints the pip command that
installs from the bundle without network access. The checksums detect accidental damage and
tampering with the media, but only if you obtained SHA256SUMS through a channel you trust.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bank_analyzer.deploy import (  # noqa: E402
    DeployError,
    install_command,
    make_bundle,
    verify_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build", help="create a bundle (needs internet)")
    build.add_argument("folder", type=Path)
    build.add_argument("--no-wheels", action="store_true", help="copy the app and checksums only")
    for name in ("verify", "install-command"):
        sub.add_parser(name).add_argument("folder", type=Path)
    args = parser.parse_args(argv)

    if args.action == "build":
        try:
            count = make_bundle(ROOT, args.folder, include_wheels=not args.no_wheels)
        except DeployError as exc:
            print(f"Bundle failed: {exc}")
            return 1
        print(f"Bundle written to {args.folder} ({count} files listed in SHA256SUMS).")
        if args.no_wheels:
            print("WARNING: no wheelhouse; the target machine cannot install packages from this.")
        return 0
    if args.action == "verify":
        problems = verify_manifest(args.folder)
        for problem in problems:
            print(f"  - {problem}")
        print(
            "VERIFIED: every file matches SHA256SUMS."
            if not problems
            else f"FAILED: {len(problems)} problem(s)."
        )
        return 0 if not problems else 1
    print(shlex.join(install_command(args.folder)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
