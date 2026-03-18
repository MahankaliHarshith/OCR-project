"""
Structured JSON Logging Configuration.

Provides machine-parseable JSON log output alongside the existing
human-readable console + file logging.  JSON logs are written to
``logs/app.json.log`` and are designed for ingestion by:
  - **Grafana Loki** (via Promtail or Docker log driver)
  - **ELK Stack** (Filebeat → Elasticsearch → Kibana)
  - **Datadog**, **Splunk**, or any JSON-log pipeline

Usage:
    from app.json_logging import setup_json_logging
    setup_json_logging()  # Call once at startup, AFTER setup_logging()

All existing text-format loggers continue to work unchanged.  This
module simply adds an *additional* handler that emits structured JSON.
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from typing import Any

from app.config import (
    LOG_DIR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
)
from app.logging_config import WindowsSafeRotatingFileHandler

# Env-var toggle: set JSON_LOGGING_ENABLED=false to disable
JSON_LOGGING_ENABLED = os.getenv("JSON_LOGGING_ENABLED", "true").lower() in ("true", "1", "yes")

# JSON log file path
JSON_LOG_FILE = LOG_DIR / "app.json.log"

# Also emit JSON to stdout (useful for Docker / container log drivers)
JSON_LOGGING_STDOUT = os.getenv("JSON_LOGGING_STDOUT", "false").lower() in ("true", "1", "yes")


class JSONFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.

    Output fields:
        timestamp   — ISO-8601 UTC
        level       — DEBUG / INFO / WARNING / ERROR / CRITICAL
        logger      — Logger name (e.g. "app.services.receipt_service")
        message     — Human-readable log message
        module      — Python module name
        function    — Function / method name
        line        — Source line number
        thread      — Thread name
        process     — Process ID
        exception   — Full traceback (if present)
        extra       — Any extra fields passed via ``logger.info("msg", extra={...})``

    Example output:
        {"timestamp":"2025-01-15T10:30:45.123Z","level":"INFO","logger":"app.api.routes","message":"Receipt scan complete","module":"routes","function":"scan_receipt","line":142}
    """

    # Fields that are part of the standard LogRecord and should NOT
    # be included in the "extra" bucket.
    _BUILTIN_ATTRS = frozenset({
        "args", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message",
        "module", "msecs", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "thread",
        "threadName", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        # Base structured fields
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Thread / process context
        if record.threadName and record.threadName != "MainThread":
            log_entry["thread"] = record.threadName
        if record.process:
            log_entry["pid"] = record.process

        # Exception info
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        # Stack info (explicit stack_info=True)
        if record.stack_info:
            log_entry["stack_info"] = self.formatStack(record.stack_info)

        # Extra fields — anything the caller passed via extra={...}
        extras = {}
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and not key.startswith("_"):
                try:
                    json.dumps(value)  # Ensure serialisable
                    extras[key] = value
                except (TypeError, ValueError):
                    extras[key] = str(value)
        if extras:
            log_entry["extra"] = extras

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_json_logging() -> None:
    """
    Add a JSON-structured log handler to the root logger.

    Call this *after* ``setup_logging()`` so the text handlers are
    already in place.  This adds a non-destructive additional handler.
    """
    if not JSON_LOGGING_ENABLED:
        logging.getLogger(__name__).debug("JSON logging disabled (JSON_LOGGING_ENABLED=false)")
        return

    root = logging.getLogger()
    formatter = JSONFormatter()

    # ── 1. JSON rotating file handler ────────────────────────────────────
    LOG_DIR.mkdir(exist_ok=True)
    json_file_handler = WindowsSafeRotatingFileHandler(
        filename=str(JSON_LOG_FILE),
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    json_file_handler.setLevel(logging.DEBUG)
    json_file_handler.setFormatter(formatter)
    json_file_handler.set_name("json_file")
    root.addHandler(json_file_handler)

    # ── 2. Optional JSON stdout handler (for Docker log drivers) ─────────
    if JSON_LOGGING_STDOUT:
        json_stdout_handler = logging.StreamHandler(sys.stdout)
        json_stdout_handler.setLevel(logging.INFO)
        json_stdout_handler.setFormatter(formatter)
        json_stdout_handler.set_name("json_stdout")
        root.addHandler(json_stdout_handler)

    log = logging.getLogger(__name__)
    log.info(
        "Structured JSON logging enabled",
        extra={
            "json_log_file": str(JSON_LOG_FILE),
            "json_stdout": JSON_LOGGING_STDOUT,
        },
    )
