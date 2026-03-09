"""
Centralized Logging Configuration.
Sets up file + console logging with rotation, colored output,
and per-module log levels for easy debugging.

Usage:
    from app.logging_config import setup_logging
    setup_logging()  # Call once at startup (main.py / run.py)
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import (
    LOG_LEVEL,
    LOG_DIR,
    LOG_FILE,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_BACKUP_COUNT,
    LOG_FORMAT,
    LOG_DATE_FORMAT,
    LOG_CONSOLE_FORMAT,
)


# ─── ANSI color codes for console output ──────────────────────────────────────
class ColoredFormatter(logging.Formatter):
    """Adds ANSI color codes to log messages based on level."""

    COLORS = {
        "DEBUG":    "\033[36m",   # Cyan
        "INFO":     "\033[32m",   # Green
        "WARNING":  "\033[33m",   # Yellow
        "ERROR":    "\033[31m",   # Red
        "CRITICAL": "\033[1;31m", # Bold Red
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging() -> None:
    """
    Configure application-wide logging with:
      - Rotating file handler  (DEBUG level, detailed format, logs/app.log)
      - Console handler        (configurable level, colored, compact format)
      - Quieter third-party loggers (easyocr, PIL, matplotlib, urllib3)
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # Capture everything; handlers filter

    # Remove any pre-existing handlers (avoid duplicates on reload)
    root.handlers.clear()

    # ── 1. Rotating File Handler (captures ALL levels) ────────────────────
    LOG_DIR.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(LOG_FILE),
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    )
    root.addHandler(file_handler)

    # ── 2. Console Handler (respects LOG_LEVEL env var) ───────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    console_handler.setFormatter(
        ColoredFormatter(LOG_CONSOLE_FORMAT, datefmt=LOG_DATE_FORMAT)
    )
    root.addHandler(console_handler)

    # ── 3. Quiet down noisy third-party loggers ───────────────────────────
    noisy_loggers = [
        "easyocr",
        "PIL",
        "matplotlib",
        "urllib3",
        "httpcore",
        "httpx",
        "multipart",
        "watchfiles",
        "torch",
    ]
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Keep uvicorn access logs at INFO (not DEBUG)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    # ── 4. Startup banner ─────────────────────────────────────────────────
    log = logging.getLogger("app.logging_config")
    log.info("=" * 60)
    log.info("Logging initialised")
    log.info(f"  Console level : {LOG_LEVEL.upper()}")
    log.info(f"  File level    : DEBUG")
    log.info(f"  Log file      : {LOG_FILE}")
    log.info(f"  Max size      : {LOG_FILE_MAX_BYTES // (1024*1024)} MB x {LOG_FILE_BACKUP_COUNT} backups")
    log.info("=" * 60)
