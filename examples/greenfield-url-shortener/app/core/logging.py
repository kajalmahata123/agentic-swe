"""Structured (JSON lines) logging for the ``app`` logger hierarchy."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_EXTRA_FIELDS = ("method", "path", "status", "duration_ms")


class JsonFormatter(logging.Formatter):
    """Format each record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Configure the ``app`` logger; safe to call repeatedly.

    A single ``StreamHandler`` using :class:`JsonFormatter` is attached only if one is
    not already present. Propagation is left enabled so pytest's ``caplog`` sees records.
    """
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level.upper())
    already_attached = any(
        isinstance(handler, logging.StreamHandler)
        and isinstance(handler.formatter, JsonFormatter)
        for handler in app_logger.handlers
    )
    if not already_attached:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        app_logger.addHandler(handler)
    app_logger.propagate = True
