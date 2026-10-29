"""Start the web UI, after checks that it will stay on this computer.

    python scripts/run_app.py [--port 8501] [--address 127.0.0.1]

Refuses to start if the address is not loopback (unless --allow-non-loopback, with a warning) or if
the configuration is unsafe. Stop with Ctrl+C in this terminal.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bank_analyzer.config import load_settings  # noqa: E402
from bank_analyzer.deploy import launch_environment, preflight  # noqa: E402
from bank_analyzer.domain.errors import ConfigError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        "--allow-non-loopback", action="store_true", help="DANGEROUS: expose the app to the network"
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}")
        return 1
    problems = preflight(
        ROOT, settings, address=args.address, allow_non_loopback=args.allow_non_loopback
    )
    if problems:
        print("Not starting:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    if args.allow_non_loopback and args.address not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"WARNING: listening on {args.address}. Anyone who can reach this port can read your data."
        )
    if importlib.util.find_spec("streamlit") is None:
        print(
            "Streamlit is not installed. Install it (see README, 'Install'), then run this again."
        )
        return 1
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(ROOT / "app.py"),
        "--server.address",
        args.address,
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    url = f"http://{args.address}:{args.port}"  # offline-ok: the local address chosen above
    print(f"Starting on {url}  (Ctrl+C to stop)")
    try:
        return subprocess.call(command, cwd=ROOT, env=launch_environment())  # noqa: S603
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
