"""Application settings.

Precedence: environment variables (``BANKAN_*``) over built-in defaults. Loading is a pure
function of its inputs so it can be tested without touching the real environment.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from bank_analyzer.domain.errors import ConfigError

_LOOPBACK_NAMES = frozenset({"localhost"})
_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "mobile documents")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved, validated settings."""

    data_dir: Path
    db_path: Path
    ollama_url: str
    model: str
    max_upload_mb_tabular: int
    max_upload_mb_pdf: int
    max_pdf_pages: int
    llm_batch_size: int
    llm_timeout_s: int
    block_outbound: bool
    allow_remote_llm: bool


def is_loopback_url(url: str) -> bool:
    """True if ``url`` is http(s) and its host is localhost or a loopback IP."""
    try:
        parsed = urlparse(url)
        host_name = parsed.hostname
    except ValueError:  # malformed, e.g. an unbalanced IPv6 bracket
        return False
    if parsed.scheme not in {"http", "https"} or not host_name:
        return False
    host = host_name
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_data_dir(env: Mapping[str, str], platform: str, home: Path) -> Path:
    """OS user-data directory, deliberately outside the source tree and common sync folders."""
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "BankAnalyzer"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "BankAnalyzer"
    xdg = env.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "bankanalyzer"


def sync_folder_warning(path: Path) -> str | None:
    """A warning if ``path`` appears to be inside a cloud-synced folder, else None."""
    lowered = [part.lower() for part in path.parts]
    for marker in _SYNC_MARKERS:
        if any(marker in part for part in lowered):
            return (
                "The data directory appears to be inside a cloud-synced folder "
                f"('{marker}'). Financial data could be uploaded. Choose a local folder."
            )
    return None


def _int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{key} must be a boolean (1/0, true/false)")


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> Settings:
    """Build Settings from the environment, rejecting unsafe values.

    A non-loopback Ollama URL is refused unless ``BANKAN_ALLOW_REMOTE_LLM`` is set, because
    prompts contain transaction text.
    """
    import sys

    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home

    data_dir = Path(env.get("BANKAN_DATA_DIR") or default_data_dir(env, platform, home))
    db_path = Path(env.get("BANKAN_DB_PATH") or data_dir / "bank.sqlite3")
    ollama_url = env.get("BANKAN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    allow_remote = _bool(env, "BANKAN_ALLOW_REMOTE_LLM", False)
    if not allow_remote and not is_loopback_url(ollama_url):
        raise ConfigError(
            "BANKAN_OLLAMA_URL must point to localhost. Remote model servers would receive "
            "transaction text and are not supported."
        )
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        ollama_url=ollama_url,
        model=env.get("BANKAN_MODEL", "llama3.2:3b"),
        max_upload_mb_tabular=_int(
            env, "BANKAN_MAX_UPLOAD_MB_CSV_XLSX", 25, minimum=1, maximum=500
        ),
        max_upload_mb_pdf=_int(env, "BANKAN_MAX_UPLOAD_MB_PDF", 50, minimum=1, maximum=500),
        max_pdf_pages=_int(env, "BANKAN_MAX_PDF_PAGES", 100, minimum=1, maximum=2000),
        llm_batch_size=_int(env, "BANKAN_LLM_BATCH_SIZE", 20, minimum=1, maximum=200),
        llm_timeout_s=_int(env, "BANKAN_LLM_TIMEOUT_S", 60, minimum=1, maximum=3600),
        block_outbound=_bool(env, "BANKAN_BLOCK_OUTBOUND", True),
        allow_remote_llm=allow_remote,
    )
