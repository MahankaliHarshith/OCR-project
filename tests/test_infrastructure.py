"""
Unit tests for infrastructure modules.

Covers:
  - logging_config.py (setup_logging, ColoredFormatter, WindowsSafeRotatingFileHandler)
  - tracing.py (setup_tracing, get_tracer, optional_span, _NoOpSpan, _NoOpTracer)
  - metrics.py (record_scan, record_azure_call, record_cache_hit/miss, etc.)
  - json_logging.py (JSONFormatter, setup_json_logging)
  - websocket.py (ConnectionManager)
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Logging Config Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestColoredFormatter:
    """Tests for the ColoredFormatter."""

    def test_format_info(self):
        from app.logging_config import ColoredFormatter
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        output = fmt.format(record)
        assert "hello" in output
        assert "\033[32m" in output  # Green for INFO

    def test_format_error(self):
        from app.logging_config import ColoredFormatter
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="fail", args=(), exc_info=None,
        )
        output = fmt.format(record)
        assert "\033[31m" in output  # Red for ERROR

    def test_format_warning(self):
        from app.logging_config import ColoredFormatter
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="warn", args=(), exc_info=None,
        )
        output = fmt.format(record)
        assert "\033[33m" in output  # Yellow for WARNING

    def test_format_debug(self):
        from app.logging_config import ColoredFormatter
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=0,
            msg="debug", args=(), exc_info=None,
        )
        output = fmt.format(record)
        assert "\033[36m" in output  # Cyan for DEBUG

    def test_format_critical(self):
        from app.logging_config import ColoredFormatter
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=logging.CRITICAL, pathname="", lineno=0,
            msg="crit", args=(), exc_info=None,
        )
        output = fmt.format(record)
        assert "\033[1;31m" in output  # Bold Red for CRITICAL


class TestWindowsSafeRotatingFileHandler:
    """Tests for the WindowsSafeRotatingFileHandler."""

    def test_creates_log_file(self, tmp_path):
        from app.logging_config import WindowsSafeRotatingFileHandler
        log_file = tmp_path / "test.log"
        handler = WindowsSafeRotatingFileHandler(
            str(log_file), maxBytes=1024, backupCount=2
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        handler.emit(record)
        handler.close()
        assert log_file.exists()
        assert "test message" in log_file.read_text()

    def test_rotation_permission_error_handled(self, tmp_path):
        """Rotation continues even if PermissionError occurs."""
        from app.logging_config import WindowsSafeRotatingFileHandler
        log_file = tmp_path / "test.log"
        handler = WindowsSafeRotatingFileHandler(
            str(log_file), maxBytes=50, backupCount=2
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        # Write enough to trigger rotation
        for i in range(20):
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg=f"message number {i:05d}", args=(), exc_info=None,
            )
            handler.emit(record)
        handler.close()
        # Should not crash — that's the test


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_creates_handlers(self, tmp_path):
        """setup_logging installs file + console handlers on root logger."""
        from app.logging_config import setup_logging

        # Patch LOG_DIR / LOG_FILE to use tmp_path
        with patch("app.logging_config.LOG_DIR", tmp_path), \
             patch("app.logging_config.LOG_FILE", tmp_path / "app.log"):
            setup_logging()

        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert any("Handler" in t for t in handler_types)

        # Cleanup handlers to avoid side effects
        root.handlers.clear()

    def test_noisy_loggers_quieted(self, tmp_path):
        """Third-party loggers are set to WARNING."""
        from app.logging_config import setup_logging

        with patch("app.logging_config.LOG_DIR", tmp_path), \
             patch("app.logging_config.LOG_FILE", tmp_path / "app.log"):
            setup_logging()

        assert logging.getLogger("easyocr").level >= logging.WARNING
        assert logging.getLogger("PIL").level >= logging.WARNING
        assert logging.getLogger("urllib3").level >= logging.WARNING

        logging.getLogger().handlers.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tracing Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNoOpSpan:
    """Tests for _NoOpSpan."""

    def test_all_methods_are_noop(self):
        from app.tracing import _NoOpSpan
        span = _NoOpSpan()
        # All these should execute without errors
        span.set_attribute("key", "value")
        span.set_status("OK")
        span.add_event("event")
        span.record_exception(ValueError("test"))
        span.end()

    def test_context_manager(self):
        from app.tracing import _NoOpSpan
        with _NoOpSpan() as span:
            span.set_attribute("key", "value")


class TestNoOpTracer:
    """Tests for _NoOpTracer."""

    def test_start_as_current_span(self):
        from app.tracing import _NoOpSpan, _NoOpTracer
        tracer = _NoOpTracer()
        span = tracer.start_as_current_span("test")
        assert isinstance(span, _NoOpSpan)

    def test_start_span(self):
        from app.tracing import _NoOpSpan, _NoOpTracer
        tracer = _NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, _NoOpSpan)


class TestGetTracer:
    """Tests for get_tracer function."""

    def test_returns_tracer(self):
        from app.tracing import get_tracer
        tracer = get_tracer("test_module")
        assert tracer is not None
        # Should have start_as_current_span method (real or no-op)
        assert hasattr(tracer, "start_as_current_span")


class TestOptionalSpan:
    """Tests for the optional_span context manager."""

    def test_disabled_tracing_yields_noop(self):
        from app.tracing import _NoOpSpan, _NoOpTracer, optional_span

        tracer = _NoOpTracer()
        with patch("app.tracing.OTEL_ENABLED", False), optional_span(tracer, "test_op") as span:
            assert isinstance(span, _NoOpSpan)

    def test_disabled_tracing_with_attributes(self):
        from app.tracing import _NoOpTracer, optional_span

        tracer = _NoOpTracer()
        with patch("app.tracing.OTEL_ENABLED", False), optional_span(tracer, "op", {"key": "val"}) as span:
            span.set_attribute("extra", "data")


class TestSetupTracing:
    """Tests for setup_tracing function."""

    def test_disabled_by_default(self):
        """setup_tracing is a no-op when OTEL_ENABLED is False."""
        import app.tracing as tracing_mod
        # Reset state
        tracing_mod._initialized = False
        tracing_mod._tracer_provider = None

        with patch.object(tracing_mod, "OTEL_ENABLED", False):
            tracing_mod.setup_tracing()
            assert tracing_mod._tracer_provider is None

        # Reset for other tests
        tracing_mod._initialized = False

    def test_get_current_trace_id_disabled(self):
        """Returns None when tracing is disabled."""
        from app.tracing import get_current_trace_id
        with patch("app.tracing.OTEL_ENABLED", False):
            assert get_current_trace_id() is None


class TestShutdownTracing:
    """Tests for shutdown_tracing."""

    def test_shutdown_no_provider(self):
        """shutdown when no provider exists is a no-op."""
        import app.tracing as tracing_mod
        tracing_mod._tracer_provider = None
        tracing_mod.shutdown_tracing()  # Should not raise

    def test_shutdown_with_mock_provider(self):
        """shutdown calls provider.shutdown()."""
        import app.tracing as tracing_mod
        mock_provider = MagicMock()
        tracing_mod._tracer_provider = mock_provider
        tracing_mod.shutdown_tracing()
        mock_provider.shutdown.assert_called_once()
        tracing_mod._tracer_provider = None  # cleanup


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metrics Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMetrics:
    """Tests for the metrics helper functions."""

    def test_record_scan_success(self):
        from app.metrics import record_scan
        # Should not raise
        record_scan(
            strategy="hybrid",
            success=True,
            duration=2.5,
            items_count=5,
            avg_confidence=0.92,
        )

    def test_record_scan_error(self):
        from app.metrics import record_scan
        record_scan(
            strategy="local",
            success=False,
            duration=1.0,
            items_count=0,
            avg_confidence=0.0,
        )

    def test_record_azure_call(self):
        from app.metrics import record_azure_call
        record_azure_call(model="prebuilt-receipt", success=True)
        record_azure_call(model="prebuilt-receipt", success=False)

    def test_update_azure_usage(self):
        from app.metrics import update_azure_usage
        update_azure_usage(daily=15, monthly=300)

    def test_record_cache_hit_miss(self):
        from app.metrics import record_cache_hit, record_cache_miss
        record_cache_hit()
        record_cache_miss()

    def test_record_rate_limit(self):
        from app.metrics import record_rate_limit
        record_rate_limit("general")
        record_rate_limit("scan")

    def test_set_db_connections(self):
        from app.metrics import set_db_connections
        set_db_connections(5)

    def test_metric_objects_exist(self):
        """All metric objects are properly initialised."""
        from app.metrics import (
            AZURE_CALLS,
            CACHE_HITS,
            CACHE_MISSES,
            CONFIDENCE_SCORE,
            DB_CONNECTIONS,
            ITEMS_DETECTED,
            RATE_LIMIT_REJECTIONS,
            SCAN_DURATION,
            SCANS_TOTAL,
        )
        assert SCANS_TOTAL is not None
        assert SCAN_DURATION is not None
        assert ITEMS_DETECTED is not None
        assert CONFIDENCE_SCORE is not None
        assert AZURE_CALLS is not None
        assert CACHE_HITS is not None
        assert CACHE_MISSES is not None
        assert DB_CONNECTIONS is not None
        assert RATE_LIMIT_REJECTIONS is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JSON Logging Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestJSONFormatter:
    """Tests for the JSONFormatter."""

    def test_basic_format(self):
        from app.json_logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname="test.py", lineno=42,
            msg="hello world", args=(), exc_info=None,
        )
        output = fmt.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "hello world"
        assert data["logger"] == "app.test"
        assert data["line"] == 42
        assert "timestamp" in data

    def test_format_with_exception(self):
        from app.json_logging import JSONFormatter
        fmt = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname="test.py", lineno=10,
            msg="error occurred", args=(), exc_info=exc_info,
        )
        output = fmt.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert data["exception"]["type"] == "ValueError"
        assert "test error" in data["exception"]["message"]

    def test_format_extra_fields(self):
        from app.json_logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="with extras", args=(), exc_info=None,
        )
        record.request_id = "req-123"
        record.user_ip = "192.168.1.1"
        output = fmt.format(record)
        data = json.loads(output)
        assert "extra" in data
        assert data["extra"]["request_id"] == "req-123"
        assert data["extra"]["user_ip"] == "192.168.1.1"

    def test_format_non_main_thread(self):
        from app.json_logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="from worker", args=(), exc_info=None,
        )
        record.threadName = "Worker-1"
        output = fmt.format(record)
        data = json.loads(output)
        assert data["thread"] == "Worker-1"

    def test_format_main_thread_omitted(self):
        from app.json_logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="main", args=(), exc_info=None,
        )
        record.threadName = "MainThread"
        output = fmt.format(record)
        data = json.loads(output)
        assert "thread" not in data


class TestSetupJsonLogging:
    """Tests for setup_json_logging."""

    def test_disabled_when_env_false(self, tmp_path):
        from app.json_logging import setup_json_logging
        with patch("app.json_logging.JSON_LOGGING_ENABLED", False):
            setup_json_logging()  # Should be a no-op

    def test_enabled_creates_handler(self, tmp_path):
        from app.json_logging import setup_json_logging
        root = logging.getLogger()
        initial_count = len(root.handlers)

        with patch("app.json_logging.JSON_LOGGING_ENABLED", True), \
             patch("app.json_logging.JSON_LOGGING_STDOUT", False), \
             patch("app.json_logging.LOG_DIR", tmp_path), \
             patch("app.json_logging.JSON_LOG_FILE", tmp_path / "app.json.log"):
            setup_json_logging()

        assert len(root.handlers) > initial_count

        # Cleanup — remove the json handler we just added
        root.handlers = [h for h in root.handlers if getattr(h, 'name', '') != 'json_file']


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebSocket Manager Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConnectionManager:
    """Tests for the WebSocket ConnectionManager."""

    def test_has_subscribers_empty(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        assert mgr.has_subscribers("batch-1") is False

    @pytest.mark.asyncio
    async def test_connect_and_has_subscribers(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect("batch-1", ws)
        ws.accept.assert_called_once()
        assert mgr.has_subscribers("batch-1") is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect("batch-1", ws)
        await mgr.disconnect("batch-1", ws)
        assert mgr.has_subscribers("batch-1") is False

    @pytest.mark.asyncio
    async def test_broadcast(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await mgr.connect("batch-1", ws1)
        await mgr.connect("batch-1", ws2)

        await mgr.broadcast("batch-1", {"type": "test"})
        ws1.send_text.assert_called_once()
        ws2.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_no_subscribers(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        # Should not raise
        await mgr.broadcast("nonexistent", {"type": "test"})

    @pytest.mark.asyncio
    async def test_broadcast_cleans_dead_clients(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        ws = AsyncMock()
        ws.send_text.side_effect = Exception("disconnected")
        await mgr.connect("batch-1", ws)
        await mgr.broadcast("batch-1", {"type": "test"})
        # Dead client should be cleaned up
        assert mgr.has_subscribers("batch-1") is False

    @pytest.mark.asyncio
    async def test_send_personal(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.send_personal(ws, {"type": "hello"})
        ws.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_batch(self):
        from app.websocket import ConnectionManager
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect("batch-1", ws)
        await mgr.close_batch("batch-1")
        ws.close.assert_called_once()
        assert mgr.has_subscribers("batch-1") is False

    def test_get_ws_manager_singleton(self):
        from app.websocket import get_ws_manager
        mgr1 = get_ws_manager()
        mgr2 = get_ws_manager()
        assert mgr1 is mgr2
