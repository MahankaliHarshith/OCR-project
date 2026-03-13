"""
OpenTelemetry Distributed Tracing for the Receipt Scanner Application.

Provides request-level visibility into the full scan pipeline:
    HTTP request → preprocessing → OCR engine → parsing → verification → DB save

Setup is environment-driven — disabled by default for zero-overhead in dev:
    OTEL_TRACING_ENABLED=true     Enable tracing
    OTEL_EXPORTER_ENDPOINT        OTLP endpoint (default: http://localhost:4317)
    OTEL_SERVICE_NAME             Service name (default: receipt-scanner)

Compatible exporters: Jaeger, Grafana Tempo, Azure Monitor, Zipkin (via OTLP).

Usage in application code:
    from app.tracing import get_tracer, optional_span

    tracer = get_tracer(__name__)

    # Context-manager span (recommended)
    with optional_span(tracer, "my_operation") as span:
        span.set_attribute("key", "value")
        result = do_work()

    # The span is automatically ended and recorded.
    # If tracing is disabled, optional_span returns a no-op context manager.
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────
OTEL_ENABLED = os.getenv("OTEL_TRACING_ENABLED", "false").lower() in ("true", "1", "yes")
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_ENDPOINT", "http://localhost:4317")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "receipt-scanner")

# ─── Global state ─────────────────────────────────────────────────────────────
_tracer_provider = None
_initialized = False


def setup_tracing(app=None):
    """
    Initialize OpenTelemetry tracing.

    Call once at application startup (inside lifespan).  If the OTel SDK
    is not installed or OTEL_TRACING_ENABLED != true, this is a no-op.

    Args:
        app: FastAPI application instance (for auto-instrumentation).
    """
    global _tracer_provider, _initialized

    if _initialized:
        return
    _initialized = True

    if not OTEL_ENABLED:
        logger.debug("OpenTelemetry tracing disabled (set OTEL_TRACING_ENABLED=true to enable)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        # Build resource with service metadata
        resource = Resource.create({
            SERVICE_NAME: OTEL_SERVICE_NAME,
            "service.version": "1.0.0",
            "deployment.environment": os.getenv("ENVIRONMENT", "development"),
        })

        # Create and register the tracer provider
        _tracer_provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(_tracer_provider)

        logger.info(
            f"   🔭 OpenTelemetry tracing enabled "
            f"(exporter={OTEL_ENDPOINT}, service={OTEL_SERVICE_NAME})"
        )

        # Auto-instrument FastAPI (creates spans for every HTTP request)
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                FastAPIInstrumentor.instrument_app(
                    app,
                    excluded_urls="health,metrics,static/.*",
                )
                logger.info("   🔭 FastAPI auto-instrumented")
            except ImportError:
                logger.debug("   opentelemetry-instrumentation-fastapi not installed")

        # Auto-instrument HTTP client calls (traces Azure API calls)
        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor
            RequestsInstrumentor().instrument()
            logger.debug("   🔭 HTTP requests auto-instrumented")
        except ImportError:
            pass

        # Auto-instrument SQLite
        try:
            from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
            SQLite3Instrumentor().instrument()
            logger.debug("   🔭 SQLite auto-instrumented")
        except ImportError:
            pass

    except ImportError as e:
        logger.debug(f"OpenTelemetry SDK not installed — tracing disabled ({e})")
    except Exception as e:
        logger.warning(f"OpenTelemetry setup failed — tracing disabled: {e}")


def shutdown_tracing():
    """Flush pending spans and shut down the tracer provider."""
    global _tracer_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
            logger.info("   🔭 OpenTelemetry tracing shut down")
        except Exception as e:
            logger.debug(f"Tracing shutdown error: {e}")


def get_tracer(name: str = __name__):
    """
    Get a tracer instance.

    Returns a real OpenTelemetry tracer if tracing is enabled,
    or the global no-op tracer if disabled.

    Args:
        name: Tracer name (typically __name__ of the calling module).

    Returns:
        opentelemetry.trace.Tracer
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


@contextmanager
def optional_span(tracer, name: str, attributes: Optional[dict] = None):
    """
    Context manager that creates a span if tracing is active, otherwise no-op.

    This is the RECOMMENDED way to add tracing to business logic.
    Zero overhead when tracing is disabled.

    Args:
        tracer: Tracer instance from get_tracer().
        name: Span name (e.g., "preprocess_image", "azure_api_call").
        attributes: Optional dict of span attributes.

    Yields:
        Span object (real or no-op).

    Example:
        tracer = get_tracer(__name__)
        with optional_span(tracer, "parse_receipt", {"items.count": 5}) as span:
            result = parser.parse(detections)
            span.set_attribute("parse.status", result["status"])
    """
    if not OTEL_ENABLED:
        yield _NoOpSpan()
        return

    try:
        from opentelemetry import trace
    except ImportError:
        yield _NoOpSpan()
        return

    try:
        with tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, v)
            yield span
    except Exception:
        # Let exceptions propagate naturally — never yield a second time
        # (yielding twice from a @contextmanager causes RuntimeError).
        raise


def get_current_trace_id() -> Optional[str]:
    """
    Get the current trace ID as a hex string (for log correlation).

    Returns None if tracing is disabled or no active span.
    """
    if not OTEL_ENABLED:
        return None
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None


# ─── No-Op Fallbacks ─────────────────────────────────────────────────────────

class _NoOpSpan:
    """No-op span when tracing is disabled. All calls are safe no-ops."""
    def set_attribute(self, key, value): pass
    def set_status(self, status, description=None): pass
    def add_event(self, name, attributes=None): pass
    def record_exception(self, exception, attributes=None): pass
    def end(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


class _NoOpTracer:
    """No-op tracer when OTel SDK is not installed."""
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()
    def start_span(self, name, **kwargs):
        return _NoOpSpan()
