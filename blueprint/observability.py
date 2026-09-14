"""Structured logging setup.

Every module logs with ``logger.info("event.name", extra={...})``. The stdlib default
formatter drops ``extra`` fields, so this module provides a JSON-lines formatter that keeps
them: one JSON object per line, greppable and loadable by the audit layer later.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Attributes every LogRecord carries; anything else on the record came from ``extra``.
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonLinesFormatter(logging.Formatter):
    """Render each record as one JSON object, including any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_file: Path, *, level: int = logging.INFO) -> None:
    """Send ``blueprint.*`` logs as JSON lines to ``log_file``.

    Only our own namespace is configured; SDK and library loggers keep their defaults so the
    log stays a clean trace of what *our* system did.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(JsonLinesFormatter())

    root = logging.getLogger("blueprint")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
