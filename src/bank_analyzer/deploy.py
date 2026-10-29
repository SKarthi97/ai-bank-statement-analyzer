"""Install, launch and offline-bundle helpers used by the scripts in ``scripts/``.

Kept in the package so they can be tested; the scripts only parse arguments and print.

* ``preflight`` refuses to start the web UI on a non-loopback address (or with unsafe settings).
* ``setup_environment`` creates the private data folder and an up-to-date database.
* The bundle functions prepare an offline install on an online machine and verify it on the offline
  one: copy the source, download the wheels, and record/check SHA-256 checksums.

The wheel download shells out to ``pip download`` and needs the internet, so it can only run on the
preparation machine; it has not been run in the environment these helpers were written in.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bank_analyzer.config import Settings
from bank_analyzer.database.maintenance import check_integrity
from bank_analyzer.database.migrations import open_database
from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.security.network_guard import is_loopback_host
from bank_analyzer.security.offline_check import check_config
from bank_analyzer.security.permissions import audit_data_dir

MANIFEST = "SHA256SUMS"
INCLUDE_DIRS = ("src", "pages", "scripts", "docs", ".streamlit")
INCLUDE_FILES = (
    "app.py",
    "app_support.py",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "PRIVACY.md",
)
_SKIP_PARTS = frozenset(
    {"__pycache__", ".venv", ".git", ".mypy_cache", ".ruff_cache", ".hypothesis"}
)


class DeployError(BankAnalyzerError):
    """A setup, launch or bundle step failed or was refused."""


# -- launching ---------------------------------------------------------------------------------


def preflight(
    root: Path, settings: Settings, *, address: str, allow_non_loopback: bool = False
) -> list[str]:
    """Reasons the web UI must not start. Empty means it is safe to launch."""
    problems: list[str] = []
    if not is_loopback_host(address) and not allow_non_loopback:
        problems.append(
            f"Refusing to listen on {address!r}: that would expose your financial data to other "
            "computers. Use 127.0.0.1, or pass --allow-non-loopback if you accept the risk."
        )
    problems += [f"{f.where}: {f.detail}" for f in check_config(root, settings)]
    return problems


# -- first-time setup --------------------------------------------------------------------------


@dataclass(slots=True)
class SetupReport:
    data_dir: Path
    db_path: Path
    schema_version: int = 0
    warnings: list[str] = field(default_factory=list)
    healthy: bool = False


def setup_environment(settings: Settings) -> SetupReport:
    """Create the private data folder and an up-to-date database, then check both."""
    report = SetupReport(settings.data_dir, settings.db_path)
    conn = open_database(settings.db_path)
    try:
        health = check_integrity(conn)
    finally:
        conn.close()
    report.schema_version = health.schema_version
    report.healthy = health.ok
    if not health.ok:
        report.warnings.append(health.summary)
    report.warnings += audit_data_dir(settings.data_dir, settings.db_path)
    return report


# -- offline bundle ----------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and p.name != MANIFEST and not (_SKIP_PARTS & set(p.parts))
    )


def write_manifest(directory: Path) -> int:
    """Write ``SHA256SUMS`` (``<hash>  <relative path>``) for every file. Returns the file count."""
    lines = [f"{sha256_file(p)}  {p.relative_to(directory).as_posix()}" for p in _files(directory)]
    (directory / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def verify_manifest(directory: Path) -> list[str]:
    """Differences between the folder and its ``SHA256SUMS``: missing, changed and extra files."""
    manifest = directory / MANIFEST
    if not manifest.is_file():
        return [f"{MANIFEST} is missing"]
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if len(digest) == 64 and name:
            expected[name] = digest
    problems: list[str] = []
    for name, digest in sorted(expected.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"missing: {name}")
        elif sha256_file(path) != digest:
            problems.append(f"changed: {name}")
    present = {p.relative_to(directory).as_posix() for p in _files(directory)}
    problems += [f"not in {MANIFEST}: {name}" for name in sorted(present - set(expected))]
    return problems


def copy_project(root: Path, dest: Path) -> int:
    """Copy the application (not data, caches or the virtual environment) into ``dest/app``."""
    target = dest / "app"
    count = 0
    for name in INCLUDE_DIRS:
        source = root / name
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            if path.is_file() and not (_SKIP_PARTS & set(path.parts)):
                out = target / path.relative_to(root)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out)
                count += 1
    for name in INCLUDE_FILES:
        source = root / name
        if source.is_file():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
            count += 1
    return count


Runner = Callable[[Sequence[str]], int]


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode  # noqa: S603


def download_wheels(requirements: Path, wheelhouse: Path, runner: Runner = _run) -> None:
    """Download every pinned wheel (and its dependencies) into ``wheelhouse``. Needs the internet.

    Wheels only (``--only-binary``): building from source on the offline machine would need a
    compiler and could run arbitrary build scripts.
    """
    wheelhouse.mkdir(parents=True, exist_ok=True)
    code = runner(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "-r",
            str(requirements),
            "-d",
            str(wheelhouse),
            "--only-binary=:all:",
        ]
    )
    if code != 0:
        raise DeployError(
            "Downloading the packages failed (pip exited with an error). The preparation machine "
            "needs internet access and the same operating system, CPU and Python version as the "
            "target machine."
        )


def make_bundle(
    root: Path, dest: Path, *, runner: Runner = _run, include_wheels: bool = True
) -> int:
    """Build an offline install folder at ``dest``: app, wheelhouse and checksums. Returns files listed."""
    if dest.exists() and any(dest.iterdir()):
        raise DeployError("The bundle folder already exists and is not empty.")
    dest.mkdir(parents=True, exist_ok=True)
    copy_project(root, dest)
    if include_wheels:
        download_wheels(root / "requirements.txt", dest / "wheelhouse", runner)
    return write_manifest(dest)


def install_command(bundle: Path) -> list[str]:
    """The command that installs from the bundle with no network access."""
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(bundle / "wheelhouse"),
        "-r",
        str(bundle / "app" / "requirements.txt"),
    ]


def launch_environment(env: Iterable[tuple[str, str]] = ()) -> dict[str, str]:
    """A copy of the environment for launching the UI, with Streamlit statistics forced off."""
    merged = dict(os.environ)
    merged.update(dict(env))
    merged["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    return merged
