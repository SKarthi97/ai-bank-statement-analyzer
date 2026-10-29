"""Static checks that the application contains nothing that talks to the internet.

``scan_static`` reads the application's own code, pages, configuration and scripts and reports
anything that looks like a remote reference: a non-loopback ``http(s)://`` URL, a script or
stylesheet pulled from elsewhere, a CDN host, or a telemetry/analytics SDK name. ``check_config``
confirms the settings that keep the app local (loopback bind, usage statistics off, XSRF
protection on, loopback model server).

What a pass proves: no such reference exists in these files. What it does not prove: that no code path
anywhere (including dependencies, Ollama, Tesseract, the browser) makes a connection. That needs the
dynamic procedure in docs/04 section 6 and an operating-system firewall.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from bank_analyzer.config import Settings, is_loopback_url

SCAN_ROOTS = ("src", "pages", ".streamlit", "scripts")
SCAN_FILES = ("app.py", "app_support.py")
SCAN_SUFFIXES = frozenset({".py", ".toml", ".html", ".js", ".css", ".sql", ".json", ".cfg"})
SELF = "offline_check.py"

_URL = re.compile(
    r"https?://(?:\[[^\]\s]*\]|[^\s\"'<>)\]},/:]+)(?::\d+)?[^\s\"'<>)\]},]*", re.IGNORECASE
)
_SCRIPT_SRC = re.compile(r"<script[^>]+src\s*=", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\s+url\(", re.IGNORECASE)
_CDN = re.compile(
    r"cdn\.|cdnjs|jsdelivr|unpkg|googleapis|gstatic|cloudflare|fontawesome|bootstrapcdn",
    re.IGNORECASE,
)
_TELEMETRY = re.compile(
    r"sentry_sdk|sentry-sdk|@sentry/|segment\.io|analytics-python|mixpanel|posthog|amplitude\.com|"
    r"amplitude-js|google-analytics|googletagmanager|ddtrace|datadoghq|newrelic|bugsnag|rollbar|"
    r"hotjar|plausible\.io|matomo\.",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str  # "url" | "script-src" | "css-import" | "cdn" | "telemetry" | "config"
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.where}: {self.detail}"


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for folder in SCAN_ROOTS:
        base = root / folder
        if base.is_dir():
            found += [
                p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES
            ]
    found += [root / n for n in SCAN_FILES if (root / n).is_file()]
    return sorted(p for p in found if p.name != SELF and "__pycache__" not in p.parts)


def scan_static(root: Path) -> list[Finding]:
    """Remote references in the application's own files (tests and docs are not scanned)."""
    findings: list[Finding] = []
    for path in _files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            findings.append(
                Finding("config", str(path.relative_to(root)), "file could not be read")
            )
            continue
        for number, line in enumerate(lines, start=1):
            where = f"{path.relative_to(root).as_posix()}:{number}"
            for match in _URL.finditer(line):
                url = match.group(0).rstrip(".;")
                if not is_loopback_url(url):
                    try:
                        host = urlparse(url).hostname or url
                    except ValueError:  # malformed URL: report it rather than crash
                        host = url
                    findings.append(Finding("url", where, host))
            if _SCRIPT_SRC.search(line):
                findings.append(Finding("script-src", where, line.strip()[:80]))
            if _CSS_IMPORT.search(line):
                findings.append(Finding("css-import", where, line.strip()[:80]))
            if _CDN.search(line):
                findings.append(Finding("cdn", where, line.strip()[:80]))
            if _TELEMETRY.search(line):
                findings.append(Finding("telemetry", where, line.strip()[:80]))
    return findings


def check_config(root: Path, settings: Settings | None = None) -> list[Finding]:
    """Problems with the settings that keep the app local. Empty means all checks passed."""
    problems: list[Finding] = []
    config = root / ".streamlit" / "config.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return [Finding("config", ".streamlit/config.toml", "missing or unreadable")]
    server, browser = data.get("server", {}), data.get("browser", {})
    address = str(server.get("address", ""))
    if address not in {"127.0.0.1", "localhost", "::1"}:
        problems.append(Finding("config", "server.address", f"must be loopback, found {address!r}"))
    if browser.get("gatherUsageStats") is not False:
        problems.append(Finding("config", "browser.gatherUsageStats", "must be false"))
    if server.get("enableXsrfProtection") is not True:
        problems.append(Finding("config", "server.enableXsrfProtection", "must be true"))
    if server.get("enableCORS") is False:
        problems.append(Finding("config", "server.enableCORS", "must not be disabled"))
    if settings is not None:
        if not settings.allow_remote_llm and not is_loopback_url(settings.ollama_url):
            problems.append(Finding("config", "ollama_url", "must be a loopback address"))
        if settings.allow_remote_llm:
            problems.append(Finding("config", "allow_remote_llm", "remote model server is allowed"))
        if not settings.block_outbound:
            problems.append(
                Finding("config", "block_outbound", "the outbound guard is switched off")
            )
    return problems
