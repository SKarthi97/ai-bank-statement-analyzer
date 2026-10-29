"""Checking that the data directory is private to the current user.

``ensure_private_dir`` sets 0700/0600 on POSIX. Windows ignores ``chmod`` for access control, so on
Windows the real ACL is read with the built-in ``icacls`` tool (a local program; no network) and
broad principals (Everyone, Users, Authenticated Users) are reported. The check only reports; it
never changes permissions. If it warns, move the data folder under your user profile (the default,
``%LOCALAPPDATA%``) or tighten the ACL yourself.

Limits: it cannot see malware running as you, an administrator, or backups and copies elsewhere,
and the group names it looks for are the English ones.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

_BROAD_PRINCIPALS = frozenset(
    {"everyone", "builtin\\users", "nt authority\\authenticated users", "users"}
)
_ACE = re.compile(r"(?:^|\s)(?P<who>[^\s:][^:]*?):(?P<rights>(?:\([^)]*\))+)\s*$")
_ACCESS_RIGHTS = frozenset({"F", "M", "W", "R", "RX", "GR", "GW", "GA", "GE", "RD", "WD", "AD"})


def parse_icacls(output: str) -> list[str]:
    """Broad principals that are granted access in ``icacls`` output (deny entries are ignored)."""
    found: list[str] = []
    for line in output.splitlines():
        match = _ACE.search(line)
        if not match:
            continue
        who = match.group("who").strip()
        groups = re.findall(r"\(([^)]*)\)", match.group("rights"))
        if "DENY" in groups:
            continue
        rights = {g for g in groups if g in _ACCESS_RIGHTS}
        if rights and who.casefold() in _BROAD_PRINCIPALS and who not in found:
            found.append(who)
    return found


def audit_private_path(path: Path) -> list[str]:
    """Warnings (empty when the path looks private). Never raises."""
    if not path.exists():
        return []
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return [f"{path.name} is readable by other users (mode {mode:o}); expected owner-only."]
        return []
    try:
        result = subprocess.run(  # noqa: S603 - fixed local executable, path passed as an argument
            ["icacls", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [f"The permissions of {path.name} could not be checked."]
    if result.returncode != 0:
        return [f"The permissions of {path.name} could not be checked."]
    broad = parse_icacls(result.stdout)
    if broad:
        return [
            f"{path.name} grants access to {', '.join(broad)}. Other accounts on this computer may "
            "be able to read your data; use a folder under your own user profile."
        ]
    return []


def audit_data_dir(data_dir: Path, db_path: Path) -> list[str]:
    """Warnings for the data directory and the database file."""
    return audit_private_path(data_dir) + audit_private_path(db_path)
