"""An optional start-up passcode for the web UI.

What it is for: keeping a casual visitor to ``http://127.0.0.1:8501`` (another person at the
keyboard, another local user, a web page that reaches the local port) from using the app. What it is
NOT: encryption. The database file stays readable by anyone who can read the file, so this does not
protect against malware, an administrator, or a stolen disk (use full-disk encryption and encrypted
backups for those).

The passcode is stored only as a scrypt hash with a random salt, compared in constant time. After
repeated wrong attempts the gate locks for a growing time, persisted in the file so restarting the
app does not reset it. A lock cannot stop someone who edits or deletes the file; it slows guessing
through the UI.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.security.file_security import ensure_private_dir, restrict_file

FILE_NAME = "passcode.json"
MIN_LENGTH = 8
FREE_ATTEMPTS = 5
BASE_LOCK_S = 30
MAX_LOCK_S = 900
DEFAULT_KDF = (2**14, 8, 1)


class PasscodeError(BankAnalyzerError):
    """The passcode cannot be set or changed (for example, too short or wrong current one)."""


class PasscodeLocked(BankAnalyzerError):
    """Too many wrong attempts; try again after ``seconds``."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"Too many wrong attempts. Try again in {seconds} seconds.")
        self.seconds = seconds


def _path(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def _hash(passcode: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passcode.encode("utf-8"))


def _load(data_dir: Path) -> dict[str, object] | None:
    try:
        data = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save(data_dir: Path, data: dict[str, object]) -> None:
    ensure_private_dir(data_dir)
    target = _path(data_dir)
    temp = target.with_name(target.name + ".part")
    temp.write_text(json.dumps(data), encoding="utf-8")
    restrict_file(temp)
    os.replace(temp, target)


def has_passcode(data_dir: Path) -> bool:
    return _load(data_dir) is not None


def set_passcode(
    data_dir: Path,
    passcode: str,
    *,
    current: str | None = None,
    kdf: tuple[int, int, int] = DEFAULT_KDF,
    now: Callable[[], float] = time.time,
) -> None:
    """Set or change the passcode. Changing an existing one requires the current passcode."""
    if has_passcode(data_dir) and not check_passcode(data_dir, current or "", now=now):
        raise PasscodeError("The current passcode is wrong.")
    if len(passcode) < MIN_LENGTH:
        raise PasscodeError(f"The passcode must be at least {MIN_LENGTH} characters.")
    n, r, p = kdf
    salt = os.urandom(16)
    _save(
        data_dir,
        {
            "n": n,
            "r": r,
            "p": p,
            "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(_hash(passcode, salt, n, r, p)).decode(),
            "failures": 0,
            "locked_until": 0,
        },
    )


def check_passcode(data_dir: Path, passcode: str, *, now: Callable[[], float] = time.time) -> bool:
    """True if correct. Raises PasscodeLocked while locked out. No passcode set means open."""
    data = _load(data_dir)
    if data is None:
        return True
    current = now()
    locked_until = float(data.get("locked_until", 0))  # type: ignore[arg-type]
    if current < locked_until:
        raise PasscodeLocked(int(locked_until - current) + 1)
    try:
        salt = base64.b64decode(str(data["salt"]))
        expected = base64.b64decode(str(data["hash"]))
        actual = _hash(passcode, salt, int(data["n"]), int(data["r"]), int(data["p"]))  # type: ignore[call-overload]
    except (KeyError, ValueError, TypeError) as exc:
        raise PasscodeError(
            "The passcode file is damaged. Remove it to reset the passcode."
        ) from exc
    if hmac.compare_digest(actual, expected):
        if data.get("failures"):
            data["failures"], data["locked_until"] = 0, 0
            _save(data_dir, data)
        return True
    failures = int(data.get("failures", 0)) + 1  # type: ignore[call-overload]
    data["failures"] = failures
    if failures >= FREE_ATTEMPTS:
        delay = min(MAX_LOCK_S, BASE_LOCK_S * 2 ** (failures - FREE_ATTEMPTS))
        data["locked_until"] = current + delay
    _save(data_dir, data)
    return False


def clear_passcode(data_dir: Path, current: str, *, now: Callable[[], float] = time.time) -> None:
    """Remove the passcode; requires the current one."""
    if not has_passcode(data_dir):
        return
    if not check_passcode(data_dir, current, now=now):
        raise PasscodeError("The current passcode is wrong.")
    _path(data_dir).unlink()
