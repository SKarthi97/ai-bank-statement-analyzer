"""The assistant: question -> plan -> execute (read-only) -> answer, with graceful degradation.

Everything that can go wrong in an expected way (no model, odd question, ambiguous currency,
unknown category) becomes a reply the user can act on, not an exception. The assistant refuses
to run on a connection that is not read-only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Literal

from bank_analyzer.ai.answer import narrate, template_answer
from bank_analyzer.ai.local_llm import LocalLLM
from bank_analyzer.ai.query_executor import AnalysisResult, NeedsClarification, QueryExecutor
from bank_analyzer.ai.query_plan import QueryPlan
from bank_analyzer.ai.query_planner import plan_question
from bank_analyzer.database.repositories import CategoryRepository
from bank_analyzer.domain.errors import ConfigError


@dataclass(frozen=True, slots=True)
class AssistantReply:
    question: str
    plan: QueryPlan | None
    plan_source: Literal["rules", "model", "none"]
    result: AnalysisResult | None
    answer: str  # plain text; statement-derived text inside it is untrusted
    answer_source: Literal["template", "model", "message"]
    options: tuple[str, ...] = ()  # choices when a clarification is needed


def require_read_only(conn: sqlite3.Connection) -> None:
    """Raise unless ``conn`` refuses writes."""
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ConfigError("The assistant needs a read-only database connection.")


class Assistant:
    def __init__(self, conn: sqlite3.Connection, today: date, llm: LocalLLM | None = None) -> None:
        require_read_only(conn)
        self._conn = conn
        self._today = today
        self._llm = llm

    def ask(self, question: str) -> AssistantReply:
        categories = [c.name for c in CategoryRepository(self._conn).list()]
        outcome = plan_question(question, self._today, categories=categories, llm=self._llm)
        if outcome.plan is None:
            return AssistantReply(question, None, "none", None, outcome.message or "", "message")
        try:
            result = QueryExecutor(self._conn, self._today).execute(outcome.plan)
        except NeedsClarification as exc:
            return AssistantReply(
                question,
                outcome.plan,
                outcome.source,
                None,
                str(exc),
                "message",
                exc.options,
            )
        answer, source = template_answer(result), "template"
        if self._llm is not None and not result.is_empty:
            narration = narrate(result, question, self._llm)
            if narration is not None:
                answer, source = narration, "model"
        return AssistantReply(
            question,
            outcome.plan,
            outcome.source,
            result,
            answer,
            source,  # type: ignore[arg-type]
        )
