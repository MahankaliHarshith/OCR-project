"""
Prometheus Metrics for the Receipt Scanner Application.

Exposes application-specific metrics at /metrics for Prometheus scraping.
Uses prometheus-fastapi-instrumentator for automatic HTTP metrics plus
custom business metrics for OCR pipeline monitoring.

Metrics exposed:
  HTTP (auto):
    - http_requests_total          — counter by method, handler, status
    - http_request_duration_seconds — histogram of response times
    - http_requests_in_progress    — gauge of concurrent requests

  Business (custom):
    - receipt_scans_total          — counter by ocr_strategy, status
    - receipt_scan_duration_seconds — histogram of full scan pipeline time
    - ocr_items_detected           — histogram of items found per scan
    - ocr_confidence_score         — histogram of average confidence per scan
    - azure_api_calls_total        — counter by model, status
    - azure_pages_used_daily       — gauge of daily Azure page consumption
    - azure_pages_used_monthly     — gauge of monthly Azure page consumption
    - cache_hits_total             — counter of image cache hits
    - cache_misses_total           — counter of image cache misses
    - db_connections_active        — gauge of active DB connections
    - rate_limit_rejections_total  — counter of 429 responses
"""

import logging

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
)

logger = logging.getLogger(__name__)

# ─── Application Info ─────────────────────────────────────────────────────────
APP_INFO = Info("receipt_scanner", "Handwritten Receipt Scanner application info")
APP_INFO.info({
    "version": "1.0.0",
    "ocr_engine": "hybrid",
})

# ─── Receipt Scan Metrics ─────────────────────────────────────────────────────
SCANS_TOTAL = Counter(
    "receipt_scans_total",
    "Total number of receipt scans",
    ["ocr_strategy", "status"],
)

SCAN_DURATION = Histogram(
    "receipt_scan_duration_seconds",
    "Time spent processing a receipt scan (full pipeline)",
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 30.0, 60.0],
)

ITEMS_DETECTED = Histogram(
    "ocr_items_detected",
    "Number of items detected per scan",
    buckets=[0, 1, 2, 3, 5, 8, 10, 15, 20, 30, 50],
)

CONFIDENCE_SCORE = Histogram(
    "ocr_confidence_score",
    "Average OCR confidence score per scan",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0],
)

# ─── Azure API Metrics ───────────────────────────────────────────────────────
AZURE_CALLS = Counter(
    "azure_api_calls_total",
    "Total Azure Document Intelligence API calls",
    ["model", "status"],
)

AZURE_PAGES_DAILY = Gauge(
    "azure_pages_used_daily",
    "Azure pages consumed today",
)

AZURE_PAGES_MONTHLY = Gauge(
    "azure_pages_used_monthly",
    "Azure pages consumed this month",
)

# ─── Cache Metrics ────────────────────────────────────────────────────────────
CACHE_HITS = Counter(
    "cache_hits_total",
    "Image cache hit count",
)

CACHE_MISSES = Counter(
    "cache_misses_total",
    "Image cache miss count",
)

# ─── Database Metrics ─────────────────────────────────────────────────────────
DB_CONNECTIONS = Gauge(
    "db_connections_active",
    "Number of active database connections",
)

# ─── Rate Limiting Metrics ────────────────────────────────────────────────────
RATE_LIMIT_REJECTIONS = Counter(
    "rate_limit_rejections_total",
    "Number of requests rejected by rate limiter",
    ["endpoint_type"],
)


# ─── Helper Functions ─────────────────────────────────────────────────────────

def record_scan(strategy: str, success: bool, duration: float,
                items_count: int, avg_confidence: float) -> None:
    """Record metrics for a completed receipt scan."""
    status = "success" if success else "error"
    SCANS_TOTAL.labels(ocr_strategy=strategy, status=status).inc()
    SCAN_DURATION.observe(duration)
    if success:
        ITEMS_DETECTED.observe(items_count)
        CONFIDENCE_SCORE.observe(avg_confidence)


def record_azure_call(model: str, success: bool) -> None:
    """Record an Azure API call."""
    status = "success" if success else "error"
    AZURE_CALLS.labels(model=model, status=status).inc()


def update_azure_usage(daily: int, monthly: int) -> None:
    """Update Azure page usage gauges."""
    AZURE_PAGES_DAILY.set(daily)
    AZURE_PAGES_MONTHLY.set(monthly)


def record_cache_hit() -> None:
    """Record an image cache hit."""
    CACHE_HITS.inc()


def record_cache_miss() -> None:
    """Record an image cache miss."""
    CACHE_MISSES.inc()


def record_rate_limit(endpoint_type: str = "general") -> None:
    """Record a rate limit rejection."""
    RATE_LIMIT_REJECTIONS.labels(endpoint_type=endpoint_type).inc()


def set_db_connections(count: int) -> None:
    """Update active DB connection gauge."""
    DB_CONNECTIONS.set(count)
