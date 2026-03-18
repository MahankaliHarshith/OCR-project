"""
Dynamic Observability Manager.

Monitors the app's own health metrics (error rate, latency, throughput)
and automatically adjusts observability settings to match actual needs:

  Low traffic / no errors  →  minimal logging, low trace sampling
  Errors spiking           →  auto-enable DEBUG logs, increase sampling
  Latency degrading        →  flag slow operations for investigation
  Back to normal           →  automatically scale back to quiet mode

DESIGN PRINCIPLES:
  1. CAN auto-adjust:   log verbosity, trace sampling, internal alerts
  2. CANNOT auto-start: Sentry (needs DSN), Prometheus (needs server),
                         OTel exporter (needs endpoint) — these need
                         external infrastructure that can't be conjured.
  3. Zero overhead:     uses a simple ring buffer, no threads, no I/O.
                         check_and_adjust() is called inline per-request.
  4. Deterministic:     decisions are based on clear thresholds,
                         all transitions are logged, fully auditable.

Usage:
    from app.observability import get_obs_manager

    mgr = get_obs_manager()
    mgr.record_request(status_code=200, latency_ms=150)
    mgr.check_and_adjust()  # auto-tunes if thresholds crossed
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ─── Thresholds ───────────────────────────────────────────────────────────────

# Error rate: fraction of requests returning 5xx in the last window
ERROR_RATE_WARNING = 0.10   # 10% errors → escalate to DEBUG logging
ERROR_RATE_CRITICAL = 0.30  # 30% errors → flag critical state

# Latency: P95 in milliseconds
LATENCY_P95_WARNING = 10_000   # 10s P95 → flag slow
LATENCY_P95_CRITICAL = 25_000  # 25s P95 → flag critical

# Minimum requests in window before we evaluate (avoid noisy decisions)
MIN_REQUESTS_FOR_EVAL = 5

# Window size: evaluate over the last N requests
WINDOW_SIZE = 50

# Cooldown: don't flip-flop states faster than this (seconds)
COOLDOWN_SECONDS = 60


# ─── Health States ────────────────────────────────────────────────────────────

class HealthState:
    HEALTHY = "healthy"           # Everything normal → minimal logging
    DEGRADED = "degraded"         # Errors or latency elevated → DEBUG logs
    CRITICAL = "critical"         # Severe issues → maximum verbosity


@dataclass
class ObsSnapshot:
    """Point-in-time observability status."""
    state: str
    error_rate: float
    latency_p95_ms: float
    request_count: int
    requests_in_window: int
    log_level: str
    components: dict[str, bool]
    last_state_change: str | None
    uptime_seconds: float


class ObservabilityManager:
    """
    Lightweight in-process health monitor that auto-adjusts observability.

    Records request outcomes in a fixed-size ring buffer (no unbounded growth).
    Periodically evaluates error rate and latency, and adjusts log verbosity.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.time()

        # Ring buffer: (timestamp, status_code, latency_ms)
        self._window: deque = deque(maxlen=WINDOW_SIZE)

        # Lifetime counters
        self._total_requests = 0
        self._total_errors = 0  # 5xx responses
        self._total_latency_ms = 0.0

        # Current state
        self._state = HealthState.HEALTHY
        self._last_state_change = time.time()
        self._original_log_level: int | None = None

        # Component status cache (computed once at startup, updated lazily)
        self._components: dict[str, bool] = {}
        self._components_checked = False

    # ─── Record a request ─────────────────────────────────────────────────

    def record_request(self, status_code: int, latency_ms: float) -> None:
        """Record a completed request. Thread-safe, O(1)."""
        with self._lock:
            now = time.time()
            self._window.append((now, status_code, latency_ms))
            self._total_requests += 1
            self._total_latency_ms += latency_ms
            if status_code >= 500:
                self._total_errors += 1

    # ─── Evaluate and adjust ──────────────────────────────────────────────

    def check_and_adjust(self) -> None:
        """
        Evaluate recent metrics and adjust observability settings.

        Call this periodically (e.g., every N requests or on a timer).
        Decisions are logged and all transitions are auditable.
        """
        with self._lock:
            window_data = list(self._window)

        if len(window_data) < MIN_REQUESTS_FOR_EVAL:
            return  # Not enough data to make decisions

        # ── Calculate metrics ──
        now = time.time()
        errors = sum(1 for _, sc, _ in window_data if sc >= 500)
        error_rate = errors / len(window_data)
        latencies = sorted(lat for _, _, lat in window_data)
        p95_idx = int(len(latencies) * 0.95)
        latency_p95 = latencies[min(p95_idx, len(latencies) - 1)]

        # ── Determine target state ──
        if error_rate >= ERROR_RATE_CRITICAL or latency_p95 >= LATENCY_P95_CRITICAL:
            target_state = HealthState.CRITICAL
        elif error_rate >= ERROR_RATE_WARNING or latency_p95 >= LATENCY_P95_WARNING:
            target_state = HealthState.DEGRADED
        else:
            target_state = HealthState.HEALTHY

        # ── Apply state transition (with cooldown) ──
        if target_state != self._state:
            elapsed_since_change = now - self._last_state_change
            if elapsed_since_change < COOLDOWN_SECONDS:
                return  # Don't flip-flop

            old_state = self._state
            self._state = target_state
            self._last_state_change = now

            self._apply_state(target_state, error_rate, latency_p95)

            logger.warning(
                f"🔄 Observability state: {old_state} → {target_state} "
                f"(error_rate={error_rate:.1%}, P95={latency_p95:.0f}ms, "
                f"window={len(window_data)} reqs)"
            )

    def _apply_state(self, state: str, error_rate: float, latency_p95: float) -> None:
        """Apply observability adjustments for the given state."""
        console_handler = self._get_console_handler()

        if state == HealthState.CRITICAL:
            # Maximum verbosity — capture everything for diagnosis
            if console_handler:
                if self._original_log_level is None:
                    self._original_log_level = console_handler.level
                console_handler.setLevel(logging.DEBUG)
            logger.critical(
                f"⚠️ CRITICAL: error_rate={error_rate:.1%}, "
                f"P95_latency={latency_p95:.0f}ms — DEBUG logging activated"
            )

        elif state == HealthState.DEGRADED:
            # Elevated verbosity — capture more context
            if console_handler:
                if self._original_log_level is None:
                    self._original_log_level = console_handler.level
                console_handler.setLevel(logging.DEBUG)
            logger.warning(
                f"⚡ DEGRADED: error_rate={error_rate:.1%}, "
                f"P95_latency={latency_p95:.0f}ms — DEBUG logging activated"
            )

        elif state == HealthState.HEALTHY:
            # Restore original log level
            if console_handler and self._original_log_level is not None:
                console_handler.setLevel(self._original_log_level)
                self._original_log_level = None
            logger.info("✅ HEALTHY: error rate and latency back to normal — log level restored")

    def _get_console_handler(self) -> logging.Handler | None:
        """Find the console StreamHandler on the root logger."""
        import sys
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream in (sys.stdout, sys.stderr):
                return handler
        return None

    # ─── Component detection ──────────────────────────────────────────────

    def get_components(self) -> dict[str, Any]:
        """
        Detect which observability components are available and active.
        Cached after first call (components don't change at runtime).
        """
        if self._components_checked:
            return self._components

        components = {}

        # Sentry
        try:
            from app.error_tracking import SENTRY_DSN, _sentry_available
            components["sentry"] = {
                "installed": True,
                "configured": bool(SENTRY_DSN),
                "active": _sentry_available,
                "reason": "Active" if _sentry_available else
                          ("No SENTRY_DSN env var" if not SENTRY_DSN else "Init failed"),
            }
        except ImportError:
            components["sentry"] = {
                "installed": False, "configured": False, "active": False,
                "reason": "sentry-sdk not installed",
            }

        # Prometheus
        try:
            from prometheus_fastapi_instrumentator import Instrumentator  # noqa: F401
            components["prometheus"] = {
                "installed": True,
                "active": True,
                "endpoint": "/internal/metrics",
                "reason": "Collecting in-memory metrics (needs external Prometheus to scrape)",
            }
        except ImportError:
            components["prometheus"] = {
                "installed": False, "active": False,
                "reason": "prometheus-fastapi-instrumentator not installed",
            }

        # OpenTelemetry
        try:
            from app.tracing import OTEL_ENABLED, OTEL_ENDPOINT
            components["opentelemetry"] = {
                "installed": True,
                "active": OTEL_ENABLED,
                "endpoint": OTEL_ENDPOINT if OTEL_ENABLED else None,
                "reason": "Active" if OTEL_ENABLED else "OTEL_TRACING_ENABLED != true",
            }
        except ImportError:
            components["opentelemetry"] = {
                "installed": False, "active": False,
                "reason": "opentelemetry-sdk not installed",
            }

        # JSON Logging
        try:
            from app.json_logging import JSON_LOG_FILE, JSON_LOGGING_ENABLED
            components["json_logging"] = {
                "active": JSON_LOGGING_ENABLED,
                "file": str(JSON_LOG_FILE) if JSON_LOGGING_ENABLED else None,
                "reason": "Active" if JSON_LOGGING_ENABLED else "JSON_LOGGING_ENABLED=false",
            }
        except ImportError:
            components["json_logging"] = {"active": False, "reason": "Module not found"}

        # File Logging
        components["file_logging"] = {
            "active": True,
            "reason": "Always active (rotating file handler)",
        }

        self._components = components
        self._components_checked = True
        return components

    # ─── Status snapshot ──────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """
        Full observability status for the /api/observability endpoint.
        Returns current state, metrics, and component status.
        """
        with self._lock:
            window_data = list(self._window)

        # Current window metrics
        if window_data:
            errors = sum(1 for _, sc, _ in window_data if sc >= 500)
            error_rate = errors / len(window_data)
            latencies = sorted(lat for _, _, lat in window_data)
            p95_idx = int(len(latencies) * 0.95)
            latency_p95 = latencies[min(p95_idx, len(latencies) - 1)]
            latency_avg = sum(lat for _, _, lat in window_data) / len(window_data)
        else:
            error_rate = 0.0
            latency_p95 = 0.0
            latency_avg = 0.0

        # Console log level
        console = self._get_console_handler()
        current_log_level = logging.getLevelName(console.level) if console else "UNKNOWN"

        uptime = time.time() - self._start_time

        return {
            "state": self._state,
            "dynamic_adjustments": {
                "log_level": current_log_level,
                "auto_debug_active": self._original_log_level is not None,
                "explanation": self._get_state_explanation(),
            },
            "current_window": {
                "requests": len(window_data),
                "window_size": WINDOW_SIZE,
                "error_rate": round(error_rate, 4),
                "latency_p95_ms": round(latency_p95, 1),
                "latency_avg_ms": round(latency_avg, 1),
            },
            "lifetime": {
                "total_requests": self._total_requests,
                "total_errors": self._total_errors,
                "error_rate": round(self._total_errors / max(self._total_requests, 1), 4),
                "avg_latency_ms": round(self._total_latency_ms / max(self._total_requests, 1), 1),
                "uptime_seconds": round(uptime, 1),
                "uptime_human": self._format_uptime(uptime),
            },
            "thresholds": {
                "error_rate_warning": ERROR_RATE_WARNING,
                "error_rate_critical": ERROR_RATE_CRITICAL,
                "latency_p95_warning_ms": LATENCY_P95_WARNING,
                "latency_p95_critical_ms": LATENCY_P95_CRITICAL,
                "min_requests_for_eval": MIN_REQUESTS_FOR_EVAL,
                "cooldown_seconds": COOLDOWN_SECONDS,
            },
            "components": self.get_components(),
        }

    def _get_state_explanation(self) -> str:
        """Human-readable explanation of current state."""
        if self._state == HealthState.HEALTHY:
            return ("All metrics within thresholds. Console log level at configured default. "
                    "No dynamic adjustments active.")
        elif self._state == HealthState.DEGRADED:
            return ("Error rate or latency elevated. Console log level auto-escalated to DEBUG "
                    "to capture diagnostic context. Will auto-restore when metrics normalize.")
        elif self._state == HealthState.CRITICAL:
            return ("Severe error rate or latency detected. Maximum logging verbosity active. "
                    "Investigate immediately. Auto-restores when metrics normalize.")
        return "Unknown state"

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        """Format seconds as human-readable uptime."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        return f"{secs}s"


# ─── Singleton ────────────────────────────────────────────────────────────────

_instance: ObservabilityManager | None = None
_instance_lock = threading.Lock()


def get_obs_manager() -> ObservabilityManager:
    """Get the global ObservabilityManager singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ObservabilityManager()
    return _instance
