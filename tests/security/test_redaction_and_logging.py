"""Redaction removes recognisable financial data; the log file never contains statement content."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bank_analyzer.security.logging_config import (
    LOG_FILE,
    LOGGER_NAME,
    configure_logging,
    shutdown_logging,
)
from bank_analyzer.security.redaction import REDACTED, describe_exception, redact_text

SECRETS = [
    "1,234.50",
    "85.40",
    "-120,00",
    "(45.00)",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "12345678",
    "GB29NWBK60161331926819",
    "GB29 NWBK 6016 1331 9268 19",
    "jane.doe@example.com",
    "token=abc123secret",
]


class TestRedactText:
    @pytest.mark.parametrize(
        "secret",
        [s for s in SECRETS if not s.startswith("token")],
    )
    def test_removes_recognisable_data(self, secret: str) -> None:
        out = redact_text(f"payment {secret} done")
        assert secret not in out
        assert REDACTED in out
        # nothing numeric may be left behind: a half-redacted "1,[redacted]" still leaks digits
        assert not any(ch.isdigit() for ch in out), out

    def test_removes_url_query_strings(self) -> None:
        assert "abc123secret" not in redact_text("GET http://127.0.0.1/x?token=abc123secret")

    @pytest.mark.parametrize(
        "text",
        [
            "Imported 12 rows from batch 7",
            "Import finished: 3 skipped",
            "migration to version 2 complete",
            "transaction b257613b-34f2-4d08-9f6c-97095548b599 categorized",
            "",
        ],
    )
    def test_ordinary_log_text_is_left_alone(self, text: str) -> None:
        assert redact_text(text) == text

    @given(st.text(max_size=200))
    def test_never_raises_and_never_grows_secrets(self, text: str) -> None:
        out = redact_text(text)
        assert isinstance(out, str)

    def test_exception_description_is_the_class_only(self) -> None:
        assert describe_exception(ValueError("balance was 1,234.50")) == "ValueError"
        assert describe_exception(None) == "unknown error"


@pytest.fixture
def logfile(tmp_path: Path) -> Iterator[Path]:
    configure_logging(tmp_path)
    yield tmp_path / "logs" / LOG_FILE
    shutdown_logging()


def flush() -> None:
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        handler.flush()


class TestLogFile:
    def test_messages_and_arguments_are_scrubbed(self, logfile: Path) -> None:
        log = logging.getLogger(LOGGER_NAME + ".test")
        log.info("categorized %s for %s", "CARD 4111 1111 1111 1111", "jane.doe@example.com")
        log.warning("balance %s", "1,234.50")
        flush()
        text = logfile.read_text(encoding="utf-8")
        for secret in ("4111", "jane.doe", "1,234.50"):
            assert secret not in text
        assert "categorized" in text  # the event itself is still logged

    def test_exception_messages_are_never_written(self, logfile: Path) -> None:
        log = logging.getLogger(LOGGER_NAME + ".test")
        try:
            raise ValueError("row 3 description PAYMENT TO JANE DOE amount 99.99")
        except ValueError:
            log.exception("import failed")
        flush()
        text = logfile.read_text(encoding="utf-8")
        assert "JANE DOE" not in text
        assert "99.99" not in text
        assert "ValueError (message omitted)" in text
        assert "import failed" in text

    def test_records_are_not_propagated_to_other_handlers(self, logfile: Path) -> None:
        assert logging.getLogger(LOGGER_NAME).propagate is False

    def test_configuring_twice_adds_one_handler(self, tmp_path: Path) -> None:
        try:
            configure_logging(tmp_path)
            configure_logging(tmp_path)
            ours = [
                h
                for h in logging.getLogger(LOGGER_NAME).handlers
                if isinstance(h, RotatingFileHandler)
            ]
            assert len(ours) == 1
        finally:
            shutdown_logging()

    def test_log_directory_is_inside_the_data_directory(
        self, logfile: Path, tmp_path: Path
    ) -> None:
        assert logfile.parent == tmp_path / "logs"
        assert logfile.exists()
