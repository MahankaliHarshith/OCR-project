"""
Production Error Tracking — Sentry Integration.

Catches unhandled exceptions, OCR pipeline failures, and slow operations
in production so you find bugs from REAL usage, not theoretical audits.

Setup:
    1. pip install sentry-sdk[fastapi]
    2. Set env var:  SENTRY_DSN=https://...@sentry.io/...
    3. That's it — Sentry auto-instruments FastAPI, SQLite, HTTP calls.

When disabled (no SENTRY_DSN), all functions are no-ops with zero overhead.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "development")
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.2"))
SENTRY_PROFILES_SAMPLE_RATE = float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1"))

_sentry_available = False


def init_sentry(app=None) -> bool:
    """
    Initialize Sentry SDK if SENTRY_DSN is set.

    Call once at app startup (in main.py lifespan).
    Returns True if Sentry was initialized, False otherwise.
    """
    global _sentry_available

    if not SENTRY_DSN:
        logger.debug("Sentry disabled (no SENTRY_DSN env var)")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.sqlite3 import Sqlite3Integration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=SENTRY_ENVIRONMENT,
            traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
                LoggingIntegration(
                    level=logging.WARNING,       # Capture WARNING+ as breadcrumbs
                    event_level=logging.ERROR,   # Send ERROR+ as Sentry events
                ),
                Sqlite3Integration(),
            ],
            # Don't send PII (filenames, IPs) unless explicitly enabled
            send_default_pii=os.getenv("SENTRY_SEND_PII", "false").lower() == "true",
            # Attach request bodies for API debugging (scan payloads are files,
            # so this mainly captures JSON bodies from confirm/update calls)
            max_request_body_size="medium",
            # Filter out health check noise
            before_send=_before_send,
            before_send_transaction=_before_send_transaction,
        )
        _sentry_available = True
        logger.info("   ✅ Sentry error tracking enabled (env=%s)", SENTRY_ENVIRONMENT)
        return True

    except ImportError:
        logger.debug("Sentry disabled (sentry-sdk not installed)")
        return False
    except Exception as e:
        logger.warning("Sentry initialization failed: %s", e)
        return False


def _before_send(event: Dict, hint: Dict) -> Optional[Dict]:
    """Filter out noisy/expected errors before sending to Sentry."""
    exc_info = hint.get("exc_info")
    if exc_info:
        exc_type = exc_info[0]
        # Don't report client disconnects or cancellations
        if exc_type and exc_type.__name__ in (
            "ConnectionResetError",
            "BrokenPipeError",
            "CancelledError",
            "ClientDisconnect",
        ):
            return None
    return event


def _before_send_transaction(event: Dict, hint: Dict) -> Optional[Dict]:
    """Filter out high-frequency low-value transactions."""
    transaction = event.get("transaction", "")
    # Skip health checks, metrics, and static files
    if transaction in ("/api/health", "/internal/metrics") or "/static/" in transaction:
        return None
    return event


# ─── Manual Error Capture ─────────────────────────────────────────────────────

def capture_exception(error: Exception = None, **context) -> Optional[str]:
    """
    Manually report an exception to Sentry with extra context.

    Usage:
        try:
            result = process_receipt(image)
        except Exception as e:
            capture_exception(e, receipt_id=123, engine="azure")

    Returns the Sentry event ID (or None if Sentry is disabled).
    """
    if not _sentry_available:
        return None
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_extra(key, value)
            return sentry_sdk.capture_exception(error)
    except Exception:
        return None


def capture_message(message: str, level: str = "info", **context) -> Optional[str]:
    """
    Send a manual message event to Sentry (for non-exception issues).

    Usage:
        capture_message("OCR returned 0 items", level="warning",
                        engine="easyocr", image_path="/tmp/img.jpg")
    """
    if not _sentry_available:
        return None
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_extra(key, value)
            return sentry_sdk.capture_message(message, level=level)
    except Exception:
        return None


def set_user(user_id: str = None, ip: str = None) -> None:
    """Set the current user context for Sentry (useful for per-IP tracking)."""
    if not _sentry_available:
        return
    try:
        import sentry_sdk
        sentry_sdk.set_user({"id": user_id, "ip_address": ip})
    except Exception:
        pass


def add_breadcrumb(message: str, category: str = "app", **data) -> None:
    """Add a breadcrumb (trail of events leading to an error)."""
    if not _sentry_available:
        return
    try:
        import sentry_sdk
        sentry_sdk.add_breadcrumb(
            message=message,
            category=category,
            data=data,
            level="info",
        )
    except Exception:
        pass


@contextmanager
def track_operation(op_name: str, **tags):
    """
    Context manager that tracks an operation and reports failures.

    Usage:
        with track_operation("ocr.azure_call", engine="receipt"):
            result = azure_engine.extract(image)

    On exception: reports to Sentry with timing + tags.
    On success: adds a breadcrumb for debugging context.
    """
    import time
    start = time.time()
    try:
        yield
        elapsed_ms = int((time.time() - start) * 1000)
        add_breadcrumb(
            f"{op_name} completed in {elapsed_ms}ms",
            category="perf",
            duration_ms=elapsed_ms,
            **tags,
        )
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        capture_exception(e, operation=op_name, duration_ms=elapsed_ms, **tags)
        raise
