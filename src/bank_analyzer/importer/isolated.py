"""Parse an untrusted file in a child process with a time limit and a memory limit.

PDF, Excel and OCR parsers handle hostile input with large native dependencies. Running them in a
separate process means a crash, a hang or runaway memory use ends that process, not the app, and a
parser's state never lives in the app's memory.

What this gives: a wall-clock timeout (the child is killed), a memory cap (POSIX: ``RLIMIT_AS`` set
by the child; Windows: a job object the parent assigns right after starting it; there is a very
short window before assignment), a minimal environment, discarded stderr, and a bounded reply that
is parsed only as JSON.

What it does not give: it is NOT a security sandbox. The child runs as the same user with the same
file access and network access; a parser exploit could still read or write anything you can. Real
containment needs an OS sandbox or a separate low-privilege account.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bank_analyzer.domain import errors
from bank_analyzer.domain.errors import BankAnalyzerError, ImportStructureError
from bank_analyzer.domain.imports import RawRow, RawTable
from bank_analyzer.domain.models import FileKind
from bank_analyzer.security.file_security import StagedFile

DEFAULT_TIMEOUT_S = 120
DEFAULT_MEMORY_MB = 1024
MAX_REPLY_BYTES = 200 * 1024 * 1024
_WORKER = "bank_analyzer.importer._worker"
_ENV_KEEP = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA")


def _child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = package_root
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# -- Windows job object (memory cap) ---------------------------------------------------------


def _assign_job_limit_windows(process: subprocess.Popen[bytes], megabytes: int) -> Any:
    """Put ``process`` in a job object with a per-process memory limit. Returns the job handle or None."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    # PROCESS_MEMORY (0x100) caps the child; KILL_ON_JOB_CLOSE (0x2000) ends it if the parent dies.
    limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
    limits.ProcessMemoryLimit = megabytes * 1024 * 1024
    ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]  # noqa: SLF001
    if not ok or not kernel32.AssignProcessToJobObject(job, handle):
        kernel32.CloseHandle(job)
        return None
    return (kernel32, job)


def _close_job(job: Any) -> None:
    if job is not None:
        job[0].CloseHandle(job[1])


# -- running a child --------------------------------------------------------------------------


def run_limited(
    command: Sequence[str],
    stdin_bytes: bytes,
    *,
    timeout_s: float,
    memory_mb: int,
) -> tuple[int, bytes, bool]:
    """Run ``command`` with the limits. Returns ``(exit_code, stdout, memory_limit_applied)``.

    Raises ImportStructureError on timeout (the child is killed). stderr is discarded.
    """
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and module, no shell
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_child_env(),
        creationflags=creationflags,
    )
    job = None
    if os.name == "nt":
        try:
            job = _assign_job_limit_windows(process, memory_mb)
        except (OSError, AttributeError, ValueError):
            job = None
    try:
        stdout, _ = process.communicate(stdin_bytes, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise ImportStructureError("Reading the file took too long and was stopped.") from exc
    finally:
        _close_job(job)
    applied = job is not None or os.name == "posix"
    return process.returncode, stdout, applied


def _raise_error(error: dict[str, Any]) -> None:
    cls = getattr(errors, str(error.get("type", "")), None)
    if isinstance(cls, type) and issubclass(cls, BankAnalyzerError):
        raise cls(str(error.get("message", "The file could not be read.")))
    raise ImportStructureError("The file could not be read as a statement.")


def _table_from_json(data: dict[str, Any]) -> RawTable:
    return RawTable(
        file_sha256=str(data["file_sha256"]),
        file_kind=FileKind(data["file_kind"]),
        headers=tuple(str(h) for h in data["headers"]),
        rows=tuple(
            RawRow(
                row_index=int(r["row_index"]),
                cells=tuple(str(c) for c in r["cells"]),
                location=r.get("location"),
                confidence=r.get("confidence"),
            )
            for r in data["rows"]
        ),
        warnings=tuple(str(w) for w in data.get("warnings", ())),
    )


def extract_table_isolated(
    staged: StagedFile,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_mb: int = DEFAULT_MEMORY_MB,
    **params: Any,
) -> RawTable:
    """Like ``extract_table`` but the parsing happens in a limited child process."""
    request = {
        "path": str(staged.path),
        "kind": staged.kind.value,
        "sha256": staged.sha256,
        "size_bytes": staged.size_bytes,
        "display_name": staged.display_name,
        "memory_mb": memory_mb,
        "params": params,
    }
    code, stdout, _ = run_limited(
        [sys.executable, "-s", "-m", _WORKER],
        json.dumps(request).encode("utf-8"),
        timeout_s=timeout_s,
        memory_mb=memory_mb,
    )
    if len(stdout) > MAX_REPLY_BYTES:
        raise ImportStructureError("The file produced more data than is allowed.")
    try:
        reply = json.loads(stdout)
    except ValueError as exc:
        # no reply: the child crashed or was killed (for example by the memory limit)
        raise ImportStructureError(
            "The file could not be read: the parser stopped unexpectedly "
            f"(exit code {code}). It may be damaged, or larger than the memory limit allows."
        ) from exc
    if not isinstance(reply, dict):
        raise ImportStructureError("The file could not be read as a statement.")
    if not reply.get("ok"):
        _raise_error(reply.get("error") or {})
    try:
        return _table_from_json(reply["table"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ImportStructureError("The file could not be read as a statement.") from exc
