"""Launch checks, first-time setup, database health check and the offline bundle tooling."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from assistant_data import load_history
from bank_analyzer.config import load_settings
from bank_analyzer.database.maintenance import check_integrity
from bank_analyzer.database.migrations import LATEST_VERSION, open_database
from bank_analyzer.database.repositories import AccountRepository
from bank_analyzer.deploy import (
    MANIFEST,
    DeployError,
    copy_project,
    download_wheels,
    install_command,
    launch_environment,
    make_bundle,
    preflight,
    setup_environment,
    sha256_file,
    verify_manifest,
    write_manifest,
)
from bank_analyzer.domain.models import AccountType

ROOT = Path(__file__).resolve().parents[2]
GOOD_CONFIG = '[server]\naddress = "127.0.0.1"\nenableXsrfProtection = true\n\n[browser]\ngatherUsageStats = false\n'


def settings_in(tmp_path: Path):  # noqa: ANN201
    return load_settings(
        {"BANKAN_DATA_DIR": str(tmp_path / "data")}, platform="win32", home=tmp_path
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".streamlit").mkdir(parents=True)
    (root / ".streamlit" / "config.toml").write_text(GOOD_CONFIG, encoding="utf-8")
    return root


class TestPreflight:
    def test_loopback_is_allowed(self, project: Path, tmp_path: Path) -> None:
        for address in ("127.0.0.1", "localhost", "::1"):
            assert preflight(project, settings_in(tmp_path), address=address) == []

    @pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.20", "example.com", "", "::"])
    def test_a_network_address_is_refused(
        self, project: Path, tmp_path: Path, address: str
    ) -> None:
        problems = preflight(project, settings_in(tmp_path), address=address)
        assert problems
        assert "expose your financial data" in problems[0]

    def test_the_override_allows_it(self, project: Path, tmp_path: Path) -> None:
        assert (
            preflight(project, settings_in(tmp_path), address="0.0.0.0", allow_non_loopback=True)
            == []
        )

    def test_unsafe_configuration_blocks_the_launch(self, project: Path, tmp_path: Path) -> None:
        (project / ".streamlit" / "config.toml").write_text(
            GOOD_CONFIG.replace("false", "true"), encoding="utf-8"
        )
        assert any(
            "gatherUsageStats" in p
            for p in preflight(project, settings_in(tmp_path), address="127.0.0.1")
        )

    def test_launch_environment_forces_statistics_off_without_touching_the_real_environment(
        self,
    ) -> None:
        env = launch_environment([("EXTRA", "1")])
        assert env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"
        assert env["EXTRA"] == "1"
        assert "EXTRA" not in os.environ


class TestSetup:
    def test_creates_a_healthy_up_to_date_database(self, tmp_path: Path) -> None:
        settings = settings_in(tmp_path)
        report = setup_environment(settings)
        assert settings.db_path.exists()
        assert report.healthy
        assert report.schema_version == LATEST_VERSION

    def test_running_again_keeps_existing_data(self, tmp_path: Path) -> None:
        settings = settings_in(tmp_path)
        setup_environment(settings)
        conn = open_database(settings.db_path)
        AccountRepository(conn).create("Keep me", AccountType.DEPOSIT, "USD")
        conn.close()
        assert setup_environment(settings).healthy
        conn = open_database(settings.db_path)
        try:
            assert [a.alias for a in AccountRepository(conn).list()] == ["Keep me"]
        finally:
            conn.close()


class TestIntegrityCheck:
    def test_a_healthy_database(self, conn: sqlite3.Connection, usd_account) -> None:  # noqa: ANN001
        load_history(conn, usd_account)
        report = check_integrity(conn)
        assert report.ok
        assert report.schema_version == LATEST_VERSION
        assert "passed" in report.summary

    def test_dangling_references_are_counted_not_shown(
        self, conn: sqlite3.Connection, usd_account
    ) -> None:  # noqa: ANN001
        load_history(conn, usd_account)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE transactions SET account_id = 'missing-account' WHERE rowid <= 3")
        report = check_integrity(conn)
        assert not report.ok
        assert report.foreign_key_violations == 3
        assert "3 row(s) refer to a missing record" in report.summary
        assert "missing-account" not in report.summary

    def test_structural_damage_is_reported(self, tmp_path: Path) -> None:
        db = tmp_path / "x.sqlite3"
        conn = open_database(db)
        load_history(conn, AccountRepository(conn).create("A", AccountType.DEPOSIT, "USD"))
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = 'CREATE INDEX idx_txn_date ON transactions(amount_minor)' "
            "WHERE name = 'idx_txn_date'"
        )
        conn.execute("PRAGMA writable_schema = OFF")
        conn.close()
        reopened = sqlite3.connect(str(db))
        try:
            report = check_integrity(reopened)
        finally:
            reopened.close()
        assert not report.ok
        assert report.problems
        assert "structural problem" in report.summary


class TestBundle:
    def make_tree(self, root: Path) -> None:
        (root / "src" / "pkg").mkdir(parents=True)
        (root / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (root / "src" / "pkg" / "__pycache__").mkdir()
        (root / "src" / "pkg" / "__pycache__" / "mod.pyc").write_bytes(b"junk")
        (root / "pages").mkdir()
        (root / "pages" / "p.py").write_text("y = 2\n", encoding="utf-8")
        (root / "data").mkdir()
        (root / "data" / "bank.sqlite3").write_bytes(b"PRIVATE FINANCIAL DATA")
        (root / ".venv").mkdir()
        (root / ".venv" / "junk.txt").write_text("venv", encoding="utf-8")
        for name in ("app.py", "requirements.txt", "README.md", "LICENSE"):
            (root / name).write_text(name + "\n", encoding="utf-8")

    def test_copy_takes_the_app_and_never_data_caches_or_the_venv(self, tmp_path: Path) -> None:
        root, dest = tmp_path / "root", tmp_path / "out"
        self.make_tree(root)
        copy_project(root, dest)
        copied = {
            p.relative_to(dest / "app").as_posix() for p in (dest / "app").rglob("*") if p.is_file()
        }
        assert copied == {
            "src/pkg/mod.py",
            "pages/p.py",
            "app.py",
            "requirements.txt",
            "README.md",
            "LICENSE",
        }
        assert not any(
            "PRIVATE" in p.read_text(errors="ignore") for p in dest.rglob("*") if p.is_file()
        )

    def test_manifest_round_trip(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("beta", encoding="utf-8")
        assert write_manifest(tmp_path) == 2
        assert verify_manifest(tmp_path) == []
        line = next(
            x for x in (tmp_path / MANIFEST).read_text().splitlines() if x.endswith("a.txt")
        )
        assert line.split("  ")[0] == sha256_file(tmp_path / "a.txt")

    def test_manifest_detects_every_kind_of_difference(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
        (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
        write_manifest(tmp_path)
        (tmp_path / "a.txt").write_text("ALPHA", encoding="utf-8")  # changed
        (tmp_path / "b.txt").unlink()  # missing
        (tmp_path / "c.txt").write_text("new", encoding="utf-8")  # extra
        assert verify_manifest(tmp_path) == [
            "changed: a.txt",
            "missing: b.txt",
            f"not in {MANIFEST}: c.txt",
        ]

    def test_a_missing_manifest_is_a_problem(self, tmp_path: Path) -> None:
        assert verify_manifest(tmp_path) == [f"{MANIFEST} is missing"]

    def test_download_uses_wheels_only_and_reports_failure(self, tmp_path: Path) -> None:
        seen: list[Sequence[str]] = []

        def runner(command: Sequence[str]) -> int:
            seen.append(command)
            return 0

        download_wheels(tmp_path / "requirements.txt", tmp_path / "wh", runner)
        command = list(seen[0])
        assert command[1:4] == ["-m", "pip", "download"]
        assert "--only-binary=:all:" in command
        assert command[command.index("-d") + 1] == str(tmp_path / "wh")
        with pytest.raises(DeployError, match="Downloading the packages failed"):
            download_wheels(tmp_path / "requirements.txt", tmp_path / "wh", lambda c: 1)

    def test_make_bundle_with_a_stand_in_downloader(self, tmp_path: Path) -> None:
        root, dest = tmp_path / "root", tmp_path / "bundle"
        self.make_tree(root)

        def fake_pip(command: Sequence[str]) -> int:
            target = Path(command[command.index("-d") + 1])
            (target / "pydantic-2.14.1-py3-none-any.whl").write_bytes(b"wheel bytes")
            return 0

        count = make_bundle(root, dest, runner=fake_pip)
        assert count == 7  # six app files + one wheel
        assert verify_manifest(dest) == []
        (dest / "wheelhouse" / "pydantic-2.14.1-py3-none-any.whl").write_bytes(b"tampered")
        assert verify_manifest(dest) == ["changed: wheelhouse/pydantic-2.14.1-py3-none-any.whl"]

    def test_a_failed_download_does_not_leave_a_bundle_that_looks_finished(
        self, tmp_path: Path
    ) -> None:
        root, dest = tmp_path / "root", tmp_path / "bundle"
        self.make_tree(root)
        with pytest.raises(DeployError):
            make_bundle(root, dest, runner=lambda c: 1)
        assert not (dest / MANIFEST).exists()

    def test_a_non_empty_destination_is_refused(self, tmp_path: Path) -> None:
        root, dest = tmp_path / "root", tmp_path / "bundle"
        self.make_tree(root)
        dest.mkdir()
        (dest / "old.txt").write_text("x", encoding="utf-8")
        with pytest.raises(DeployError, match="not empty"):
            make_bundle(root, dest, include_wheels=False)

    def test_install_command_never_touches_the_network(self, tmp_path: Path) -> None:
        command = install_command(tmp_path)
        assert "--no-index" in command
        assert command[command.index("--find-links") + 1] == str(tmp_path / "wheelhouse")


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        timeout=120,
        env=merged,
        check=False,
    )


class TestScripts:
    def test_run_app_refuses_a_network_address(self) -> None:
        result = run_script("run_app.py", "--address", "0.0.0.0")
        assert result.returncode == 1
        assert "Not starting" in result.stdout
        assert "expose your financial data" in result.stdout

    @pytest.mark.skipif(
        importlib.util.find_spec("streamlit") is not None, reason="Streamlit is installed"
    )
    def test_run_app_explains_a_missing_streamlit(self) -> None:
        result = run_script("run_app.py")
        assert result.returncode == 1
        assert "Streamlit is not installed" in result.stdout

    def test_setup_environment_script(self, tmp_path: Path) -> None:
        env = {"BANKAN_DATA_DIR": str(tmp_path / "d")}
        first = run_script("setup_environment.py", env=env)
        assert first.returncode == 0, first.stdout
        assert "Ready." in first.stdout
        assert (tmp_path / "d" / "bank.sqlite3").exists()
        assert run_script("setup_environment.py", env=env).returncode == 0

    def test_setup_environment_warns_about_a_synced_folder(self, tmp_path: Path) -> None:
        result = run_script(
            "setup_environment.py", env={"BANKAN_DATA_DIR": str(tmp_path / "OneDrive - X" / "d")}
        )
        assert "cloud-synced" in result.stdout

    def test_bundle_build_verify_and_tamper(self, tmp_path: Path) -> None:
        bundle = tmp_path / "bundle"
        built = run_script("make_offline_bundle.py", "build", str(bundle), "--no-wheels")
        assert built.returncode == 0, built.stdout
        assert "no wheelhouse" in built.stdout
        assert (bundle / "app" / "src" / "bank_analyzer" / "deploy.py").is_file()
        assert not (bundle / "app" / "data").exists()
        assert run_script("make_offline_bundle.py", "verify", str(bundle)).returncode == 0
        (bundle / "app" / "app.py").write_text("print('tampered')", encoding="utf-8")
        verdict = run_script("make_offline_bundle.py", "verify", str(bundle))
        assert verdict.returncode == 1
        assert "changed: app/app.py" in verdict.stdout

    def test_install_command_script(self, tmp_path: Path) -> None:
        result = run_script("make_offline_bundle.py", "install-command", str(tmp_path))
        assert result.returncode == 0
        assert "--no-index" in result.stdout
