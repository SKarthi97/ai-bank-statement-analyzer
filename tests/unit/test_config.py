from __future__ import annotations

from pathlib import Path

import pytest

from bank_analyzer.config import (
    default_data_dir,
    is_loopback_url,
    load_settings,
    sync_folder_warning,
)
from bank_analyzer.domain.errors import ConfigError

HOME = Path("/home/alice")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
        "http://127.5.5.5:1",
        "https://localhost",
    ],
)
def test_loopback_urls_accepted(url: str) -> None:
    assert is_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:11434",
        "http://0.0.0.0:11434",
        "http://example.com",
        "http://localhost.evil.com",
        "ftp://127.0.0.1",
        "127.0.0.1:11434",
        "http://",
        "",
    ],
)
def test_non_loopback_urls_rejected(url: str) -> None:
    assert not is_loopback_url(url)


def test_defaults() -> None:
    s = load_settings({}, platform="linux", home=HOME)
    assert s.ollama_url == "http://127.0.0.1:11434"
    assert s.block_outbound is True
    assert s.allow_remote_llm is False
    assert s.data_dir == HOME / ".local" / "share" / "bankanalyzer"
    assert s.db_path == s.data_dir / "bank.sqlite3"
    assert (s.max_upload_mb_tabular, s.max_upload_mb_pdf) == (25, 50)


def test_remote_llm_refused_by_default() -> None:
    with pytest.raises(ConfigError):
        load_settings({"BANKAN_OLLAMA_URL": "http://10.0.0.5:11434"}, platform="linux", home=HOME)


def test_remote_llm_requires_explicit_override() -> None:
    s = load_settings(
        {"BANKAN_OLLAMA_URL": "http://10.0.0.5:11434/", "BANKAN_ALLOW_REMOTE_LLM": "1"},
        platform="linux",
        home=HOME,
    )
    assert s.allow_remote_llm and s.ollama_url == "http://10.0.0.5:11434"


def test_overrides_and_validation() -> None:
    s = load_settings(
        {"BANKAN_DATA_DIR": "/x/data", "BANKAN_LLM_BATCH_SIZE": "5", "BANKAN_BLOCK_OUTBOUND": "0"},
        platform="linux",
        home=HOME,
    )
    assert s.data_dir == Path("/x/data") and s.llm_batch_size == 5 and s.block_outbound is False
    with pytest.raises(ConfigError):
        load_settings({"BANKAN_LLM_BATCH_SIZE": "abc"}, platform="linux", home=HOME)
    with pytest.raises(ConfigError):
        load_settings({"BANKAN_LLM_BATCH_SIZE": "0"}, platform="linux", home=HOME)
    with pytest.raises(ConfigError):
        load_settings({"BANKAN_BLOCK_OUTBOUND": "maybe"}, platform="linux", home=HOME)


def test_platform_data_dirs() -> None:
    assert default_data_dir({"LOCALAPPDATA": "C:/L"}, "win32", HOME) == Path("C:/L/BankAnalyzer")
    assert default_data_dir({}, "darwin", HOME) == (
        HOME / "Library" / "Application Support" / "BankAnalyzer"
    )
    assert default_data_dir({"XDG_DATA_HOME": "/xdg"}, "linux", HOME) == Path("/xdg/bankanalyzer")


def test_sync_folder_warning() -> None:
    risky = Path("C:/Users/a/OneDrive - Corp/Desktop/data")
    assert sync_folder_warning(risky) is not None
    assert sync_folder_warning(Path("C:/Users/a/AppData/Local/BankAnalyzer")) is None


def test_tesseract_command_comes_from_the_environment() -> None:
    home = Path("C:/Users/x")
    assert load_settings({}, platform="win32", home=home).tesseract_cmd is None
    blank = load_settings({"BANKAN_TESSERACT_CMD": "  "}, platform="win32", home=home)
    assert blank.tesseract_cmd is None
    chosen = load_settings(
        {"BANKAN_TESSERACT_CMD": "C:/Program Files/Tesseract-OCR/tesseract.exe"},
        platform="win32",
        home=home,
    )
    assert chosen.tesseract_cmd == "C:/Program Files/Tesseract-OCR/tesseract.exe"
