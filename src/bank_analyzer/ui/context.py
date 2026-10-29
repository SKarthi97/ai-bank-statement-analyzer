"""Opening the database, the local model and the assistant for a UI session."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from bank_analyzer.ai.local_llm import LocalLLM, OllamaClient
from bank_analyzer.config import Settings, load_settings, sync_folder_warning
from bank_analyzer.database.connection import connect_readonly
from bank_analyzer.database.migrations import open_database
from bank_analyzer.domain.errors import ConfigError

AI_OFFLINE = "AI offline: import, search, rules and analytics still work."


@dataclass(frozen=True, slots=True)
class AiStatus:
    available: bool
    model: str
    message: str


class AppContext:
    """One user's session: a read-write connection for the app and read-only ones for the assistant."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection = open_database(settings.db_path)

    @property
    def sync_warning(self) -> str | None:
        return sync_folder_warning(self.settings.data_dir)

    def llm(self) -> LocalLLM | None:
        """The local model client, or None if it cannot be created (never raises)."""
        try:
            return OllamaClient(
                self.settings.ollama_url,
                self.settings.model,
                timeout_s=self.settings.llm_timeout_s,
                allow_remote=self.settings.allow_remote_llm,
            )
        except ConfigError:
            return None

    def ai_status(self) -> AiStatus:
        llm = self.llm()
        if llm is None:
            return AiStatus(False, self.settings.model, AI_OFFLINE)
        if llm.is_available():
            return AiStatus(True, llm.model, f"AI ready ({llm.model}, running locally).")
        return AiStatus(False, llm.model, AI_OFFLINE)

    @contextmanager
    def readonly(self) -> Iterator[sqlite3.Connection]:
        """A connection that cannot write, closed afterwards. Used only by the assistant."""
        conn = connect_readonly(self.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()


def open_context(settings: Settings | None = None) -> AppContext:
    return AppContext(settings or load_settings())


def today() -> date:
    return date.today()
