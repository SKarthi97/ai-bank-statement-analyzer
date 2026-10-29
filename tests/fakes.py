"""Test doubles: a fake Ollama HTTP server, scripted in-process models, and a transaction loader.

The fake server speaks the subset of Ollama's REST API the client uses (``GET /api/tags`` and
``POST /api/chat``). It exists because no real Ollama is available in the test environment, so a
test passing here shows the client behaves correctly against this shape, not against a real server.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from bank_analyzer.ai.local_llm import LLMResponseError, LLMUnavailable
from bank_analyzer.database.repositories import ImportRepository
from bank_analyzer.domain.models import (
    CanonicalTransaction,
    FileKind,
    ImportBatch,
    SourceRef,
    new_id,
    utc_now_iso,
)
from bank_analyzer.services.normalization import merchant_key, normalize_description

# What the "model" answers for descriptions that built-in rules do not cover.
KEYWORD_CATEGORIES = {
    "BOOKSHOP": "Shopping",
    "PHYSIO": "Healthcare",
    "GYM": "Entertainment",
    "PLUMBER": "Housing",
}


def keyword_reply(user_message: str) -> dict[str, Any]:
    """A deterministic stand-in for a model: classify by keyword, otherwise say NEEDS_REVIEW."""
    lines = user_message.split("\n")
    allowed = json.loads(lines[0].split(": ", 1)[1])
    items = json.loads(lines[-1])
    results = []
    for item in items:
        category = next((c for k, c in KEYWORD_CATEGORIES.items() if k in item["desc"]), None)
        if category in allowed:
            results.append(
                {"i": item["i"], "category": category, "needs_review": False, "reason": "keyword"}
            )
        else:
            results.append(
                {"i": item["i"], "category": "NEEDS_REVIEW", "needs_review": True, "reason": ""}
            )
    return {"results": results}


class FakeOllama:
    """An in-process HTTP server on 127.0.0.1 (ephemeral port) imitating Ollama."""

    def __init__(
        self,
        *,
        models: tuple[str, ...] = ("llama3.2:3b",),
        chat: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.models = list(models)
        self.chat_handler = chat or self._default_chat
        self.requests: list[dict[str, Any]] = []
        self.tags_override: bytes | None = None  # serve this raw body for /api/tags
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(
                self, status: int, body: bytes, headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append({"method": "GET", "path": self.path, "json": None})
                if self.path == "/api/tags":
                    body = (
                        outer.tags_override
                        or json.dumps({"models": [{"name": m} for m in outer.models]}).encode()
                    )
                    self._send(200, body)
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"method": "POST", "path": self.path, "json": payload})
                if self.path != "/api/chat":
                    self._send(404, b'{"error":"not found"}')
                    return
                result = outer.chat_handler(payload)
                if isinstance(result, tuple):  # (status, body bytes, headers)
                    self._send(*result)
                else:
                    self._send(200, json.dumps(result).encode())

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @staticmethod
    def wrap(content: object) -> dict[str, Any]:
        """Wrap a model reply the way Ollama's /api/chat does (content is a JSON string)."""
        text = content if isinstance(content, str) else json.dumps(content)
        return {"model": "fake", "message": {"role": "assistant", "content": text}, "done": True}

    def _default_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.wrap(keyword_reply(payload["messages"][1]["content"]))

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def chat_requests(self) -> list[dict[str, Any]]:
        return [r["json"] for r in self.requests if r["method"] == "POST"]

    def __enter__(self) -> FakeOllama:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class KeywordLLM:
    """In-process model that classifies by keyword (see ``keyword_reply``)."""

    model = "keyword-fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return True

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> object:
        self.calls.append((system, user))
        return keyword_reply(user)


class ScriptedLLM:
    """Returns the scripted replies in order; an Exception in the script is raised instead."""

    model = "scripted-fake"

    def __init__(self, *replies: object) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return True

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> object:
        self.calls.append((system, user))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class DownLLM:
    """A model server that is not running."""

    model = "down"

    def is_available(self) -> bool:
        return False

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> object:
        raise LLMUnavailable("The local model server could not be reached.")


class BrokenLLM(ScriptedLLM):
    """Reachable but always answers garbage."""

    def __init__(self) -> None:
        super().__init__(LLMResponseError("The model did not return valid JSON."))


def load_transactions(
    conn: sqlite3.Connection, account_id: str, specs: list[dict[str, Any]]
) -> list[CanonicalTransaction]:
    """Insert synthetic transactions as one import batch; spec keys: desc, amount, day, currency."""
    now = utc_now_iso()
    batch_id = new_id()
    txns = []
    for position, spec in enumerate(specs, start=1):
        normalized = normalize_description(spec["desc"])
        txns.append(
            CanonicalTransaction(
                id=new_id(),
                account_id=account_id,
                import_batch_id=batch_id,
                txn_date=date.fromisoformat(spec.get("day", "2024-01-15")),
                original_description=spec["desc"],
                normalized_description=normalized,
                merchant_key=merchant_key(normalized),
                amount_minor=spec.get("amount", -1000),
                currency=spec.get("currency", "USD"),
                reported_balance_minor=spec.get("balance"),
                source_ref=SourceRef(file_sha256="c" * 64, row_index=position),
                created_at=now,
                updated_at=now,
            )
        )
    batch = ImportBatch(
        id=batch_id,
        account_id=account_id,
        file_sha256="c" * 64,
        file_kind=FileKind.CSV,
        row_count_read=len(txns),
        row_count_imported=len(txns),
        row_count_skipped=0,
        created_at=now,
    )
    ImportRepository(conn).commit_import(batch, tuple(txns))
    wanted = [(t, s["category"]) for t, s in zip(txns, specs, strict=True) if s.get("category")]
    if wanted:
        from bank_analyzer.database.repositories import CategoryRepository, TransactionRepository

        categories, repo = CategoryRepository(conn), TransactionRepository(conn)
        for txn, name in wanted:
            repo.set_category_manual(txn.id, categories.get_by_name(name).id)
    return txns
