"""Child-process entry point for parsing one staged file. Not for direct use.

Reads one JSON request on stdin and writes one JSON reply on stdout. Anything the parsers print is
sent to stderr (which the parent discards), so stdout carries only the reply. Application errors are
reported by type and message (their messages never contain statement content by contract); any other
exception is reported generically, because its message could contain file content.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

from bank_analyzer.domain import errors
from bank_analyzer.domain.errors import BankAnalyzerError
from bank_analyzer.domain.models import FileKind
from bank_analyzer.importer import extract_table
from bank_analyzer.security.file_security import StagedFile

_PARAMS = (
    "sheet",
    "encoding",
    "delimiter",
    "header_row",
    "max_pdf_pages",
    "use_ocr",
    "tesseract_cmd",
    "password",
)


def _limit_memory(megabytes: int) -> None:
    try:
        # POSIX only; the parent limits memory on Windows through a job object
        resource = importlib.import_module("resource")

        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass


def _table_to_json(table: Any) -> dict[str, Any]:
    return {
        "file_sha256": table.file_sha256,
        "file_kind": table.file_kind.value,
        "headers": list(table.headers),
        "rows": [
            {
                "row_index": r.row_index,
                "cells": list(r.cells),
                "location": r.location,
                "confidence": r.confidence,
            }
            for r in table.rows
        ],
        "warnings": list(table.warnings),
        "context_text": table.context_text,
    }


def run(request: dict[str, Any]) -> dict[str, Any]:
    _limit_memory(int(request.get("memory_mb", 0)) or 1024)
    staged = StagedFile(
        path=Path(request["path"]),
        kind=FileKind(request["kind"]),
        sha256=request["sha256"],
        size_bytes=int(request["size_bytes"]),
        display_name=request.get("display_name", ""),
    )
    params = {k: v for k, v in request.get("params", {}).items() if k in _PARAMS}
    try:
        return {"ok": True, "table": _table_to_json(extract_table(staged, **params))}
    except BankAnalyzerError as exc:
        name = type(exc).__name__
        known = isinstance(getattr(errors, name, None), type)
        return {
            "ok": False,
            "error": {"type": name if known else "ImportStructureError", "message": str(exc)},
        }
    except MemoryError:
        return {
            "ok": False,
            "error": {
                "type": "ImportStructureError",
                "message": "The file needs more memory than is allowed.",
            },
        }
    except Exception:  # noqa: BLE001 - the message may contain file content, so it is not forwarded
        return {
            "ok": False,
            "error": {
                "type": "ImportStructureError",
                "message": "The file could not be read as a statement.",
            },
        }


def main() -> None:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        reply = run(json.loads(sys.stdin.read()))
    except Exception:  # noqa: BLE001
        reply = {
            "ok": False,
            "error": {
                "type": "ImportStructureError",
                "message": "The file could not be read as a statement.",
            },
        }
    finally:
        sys.stdout = real_stdout
    sys.stdout.write(json.dumps(reply))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
