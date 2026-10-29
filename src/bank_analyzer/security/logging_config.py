"""Application logging that cannot leak statement content.

Every record passes through ``ScrubFilter`` (message redacted, arguments flattened) and exceptions
are written without their messages. The log goes to a size-limited file in the private data
directory; it is not propagated to the root logger, so no other handler (a framework's console
handler, say) receives the unscrubbed record. Call ``configure_logging`` once per process;
repeated calls do not add duplicate handlers.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from bank_analyzer.security.file_security import ensure_private_dir, restrict_file
from bank_analyzer.security.redaction import format_exc_info, redact_text

LOGGER_NAME = "bank_analyzer"
LOG_FILE = "bankanalyzer.log"
_MAX_BYTES = 1_000_000
_BACKUPS = 3
_MARK = "_bankanalyzer_handler"


class ScrubFilter(logging.Filter):
    """Redacts the formatted message and drops the raw arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        return True


class ScrubFormatter(logging.Formatter):
    """Formats exceptions as class and location only."""

    def formatException(self, ei: object) -> str:  # noqa: N802 - logging API name
        return format_exc_info(ei)  # type: ignore[arg-type]


def configure_logging(data_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """Attach the scrubbed rotating file handler to the ``bank_analyzer`` logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if any(getattr(h, _MARK, False) for h in logger.handlers):
        return logger
    log_dir = ensure_private_dir(data_dir / "logs")
    path = log_dir / LOG_FILE
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(ScrubFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(ScrubFilter())
    setattr(handler, _MARK, True)
    logger.addHandler(handler)
    restrict_file(path)
    return logger


def shutdown_logging() -> None:
    """Detach and close the handler (used by tests and when wiping data)."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _MARK, False):
            logger.removeHandler(handler)
            handler.close()
