"""The application's own files contain no remote references, and the scanner really finds them."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from bank_analyzer.config import load_settings
from bank_analyzer.security.offline_check import check_config, scan_static

ROOT = Path(__file__).resolve().parents[2]


def test_the_repository_has_no_remote_references() -> None:
    assert [str(f) for f in scan_static(ROOT)] == []


def test_the_repository_configuration_is_local_only() -> None:
    settings = load_settings({}, platform="win32", home=Path("C:/Users/x"))
    assert [str(f) for f in check_config(ROOT, settings)] == []


def test_the_streamlit_config_is_what_the_docs_promise() -> None:
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["address"] == "127.0.0.1"
    assert config["browser"]["gatherUsageStats"] is False
    assert config["server"]["enableXsrfProtection"] is True


def test_the_script_passes_and_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_offline.py")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert "RESULT: PASS" in result.stdout
    assert "does not prove" in result.stdout  # the limit is always stated


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / ".streamlit").mkdir()
    (tmp_path / ".streamlit" / "config.toml").write_text(
        '[server]\naddress = "127.0.0.1"\nenableXsrfProtection = true\n\n'
        "[browser]\ngatherUsageStats = false\n",
        encoding="utf-8",
    )
    return tmp_path


class TestScannerFindsOffenders:
    @pytest.mark.parametrize(
        ("line", "kind"),
        [
            ('URL = "https://api.example.com/v1"', "url"),
            ("requests.get('http://203.0.113.7/data')", "url"),
            ('<script src="/static/app.js"></script>', "script-src"),
            ("@import url(https://fonts.example/css)", "css-import"),
            ("# loaded from cdn.jsdelivr.net", "cdn"),
            ("import sentry_sdk", "telemetry"),
            ("posthog.capture('x')", "telemetry"),
        ],
    )
    def test_detects(self, project: Path, line: str, kind: str) -> None:
        (project / "src" / "bad.py").write_text(line + "\n", encoding="utf-8")
        kinds = {f.kind for f in scan_static(project)}
        assert kind in kinds

    @pytest.mark.parametrize(
        "line",
        [
            'URL = "http://127.0.0.1:11434"',
            'URL = "http://localhost:8501/path"',
            'URL = "http://[::1]:8501"',
            "_EARLIEST_PLAUSIBLE = 1990",
            "# a comment about transactions",
        ],
    )
    def test_ignores_local_and_ordinary_text(self, project: Path, line: str) -> None:
        (project / "src" / "fine.py").write_text(line + "\n", encoding="utf-8")
        assert scan_static(project) == []

    def test_reports_the_file_and_line(self, project: Path) -> None:
        (project / "src" / "bad.py").write_text(
            'x = 1\nU = "https://evil.example/x"\n', encoding="utf-8"
        )
        (finding,) = scan_static(project)
        assert finding.where == "src/bad.py:2"
        assert finding.detail == "evil.example"


class TestConfigChecks:
    def write(self, project: Path, text: str) -> None:
        (project / ".streamlit" / "config.toml").write_text(text, encoding="utf-8")

    def test_good_config(self, project: Path) -> None:
        assert check_config(project) == []

    @pytest.mark.parametrize(
        ("config", "needle"),
        [
            (
                '[server]\naddress = "0.0.0.0"\nenableXsrfProtection = true\n[browser]\ngatherUsageStats = false\n',
                "server.address",
            ),
            (
                "[server]\nenableXsrfProtection = true\n[browser]\ngatherUsageStats = false\n",
                "server.address",
            ),
            (
                '[server]\naddress = "127.0.0.1"\nenableXsrfProtection = true\n[browser]\ngatherUsageStats = true\n',
                "gatherUsageStats",
            ),
            (
                '[server]\naddress = "127.0.0.1"\nenableXsrfProtection = false\n[browser]\ngatherUsageStats = false\n',
                "enableXsrfProtection",
            ),
            (
                '[server]\naddress = "127.0.0.1"\nenableXsrfProtection = true\nenableCORS = false\n[browser]\ngatherUsageStats = false\n',
                "enableCORS",
            ),
            ("not toml [[[", "unreadable"),
        ],
    )
    def test_bad_config(self, project: Path, config: str, needle: str) -> None:
        self.write(project, config)
        assert any(needle in str(f) for f in check_config(project))

    def test_missing_config(self, tmp_path: Path) -> None:
        assert any("missing" in str(f) for f in check_config(tmp_path))

    def test_unsafe_settings_are_reported(self, project: Path) -> None:
        env = {
            "BANKAN_OLLAMA_URL": "http://10.0.0.9:11434",
            "BANKAN_ALLOW_REMOTE_LLM": "1",
            "BANKAN_BLOCK_OUTBOUND": "0",
        }
        settings = load_settings(env, platform="win32", home=Path("C:/Users/x"))
        text = " ".join(str(f) for f in check_config(project, settings))
        assert "allow_remote_llm" in text
        assert "block_outbound" in text

    def test_a_remote_model_url_is_reported_when_remote_use_was_not_allowed(
        self, project: Path
    ) -> None:
        import dataclasses

        base = load_settings({}, platform="win32", home=Path("C:/Users/x"))
        unsafe = dataclasses.replace(base, ollama_url="http://10.0.0.9:11434")
        assert any("ollama_url" in str(f) for f in check_config(project, unsafe))

    def test_malformed_urls_do_not_crash_the_scan(self, project: Path) -> None:
        (project / "src" / "odd.py").write_text(
            'X = "http://[bad"\nY = "http://]x"\n', encoding="utf-8"
        )
        assert isinstance(scan_static(project), list)
