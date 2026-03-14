"""
Main FastAPI Application.
Entry point for the Handwritten Receipt Scanner API.
"""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.logging_config import setup_logging
from app.config import (
    APP_TITLE,
    APP_VERSION,
    CORS_ORIGINS,
    UPLOAD_DIR,
    EXPORT_DIR,
    RATE_LIMIT_RPM,
    RATE_LIMIT_SCAN_RPM,
    API_SECRET_KEY,
    API_DOCS_ENABLED,
)
from app.api.routes import router
from app.middleware import (
    SecurityHeadersMiddleware,
    RateLimitMiddleware,
    APIKeyMiddleware,
    DevTunnelCORSMiddleware,
)

# ─── Logging Setup ────────────────────────────────────────────────────────────
setup_logging()

# ─── Structured JSON Logging (for Loki / ELK) ────────────────────────────────
from app.json_logging import setup_json_logging
setup_json_logging()

logger = logging.getLogger(__name__)


# ─── Lifespan (startup + shutdown) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern lifespan handler — replaces deprecated on_event."""
    # ── STARTUP ──
    from app.config import LOG_FILE
    logger.info(f"🚀 {APP_TITLE} v{APP_VERSION} starting up...")
    logger.info(f"   Uploads : {UPLOAD_DIR}")
    logger.info(f"   Exports : {EXPORT_DIR}")
    logger.info(f"   Log file: {LOG_FILE}")
    logger.info(f"   API Docs: http://localhost:8000/docs")
    logger.info(f"   Debug logs: tail -f {LOG_FILE}")

    # ── Sentry Error Tracking (production) ──
    from app.error_tracking import init_sentry
    init_sentry(app)

    # Warn about security config
    if not API_SECRET_KEY:
        logger.warning("   ⚠️  API_SECRET_KEY is empty — admin/destructive endpoints are UNPROTECTED. "
                        "Set API_SECRET_KEY env var in production.")

    # Clean up old upload files (keep last 7 days)
    try:
        import time as _time
        cutoff = _time.time() - (7 * 24 * 3600)
        cleaned = 0
        for f in UPLOAD_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                cleaned += 1
        if cleaned:
            logger.info(f"   🧹 Cleaned {cleaned} old upload files (>7 days)")
    except Exception as e:
        logger.debug(f"   Upload cleanup skipped: {e}")
    # Clean up old export files too (prevents disk leak)
    try:
        import time as _time2
        cutoff2 = _time2.time() - (7 * 24 * 3600)
        cleaned2 = 0
        for f in EXPORT_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff2:
                f.unlink()
                cleaned2 += 1
        if cleaned2:
            logger.info(f"   🧹 Cleaned {cleaned2} old export files (>7 days)")
    except Exception as e:
        logger.debug(f"   Export cleanup skipped: {e}")

    # Pre-initialize OCR engine at startup (includes model loading + warmup)
    from app.ocr.hybrid_engine import get_hybrid_engine
    from app.config import OCR_ENGINE_MODE, AZURE_DOC_INTEL_AVAILABLE

    hybrid = get_hybrid_engine()
    logger.info(f"   OCR Mode : {OCR_ENGINE_MODE}")
    logger.info(f"   Azure    : {'✅ configured' if AZURE_DOC_INTEL_AVAILABLE else '⚠ not configured (using local EasyOCR)'}")

    if OCR_ENGINE_MODE != "azure":
        from app.ocr.engine import get_ocr_engine
        logger.info("   Loading local OCR engine (one-time)...")
        get_ocr_engine()
        logger.info("   ✅ Local OCR engine ready")

        # Pre-warm second OCR reader in background thread if parallel dual-pass
        # is enabled — eliminates the 5-8s cold-start spike on the first
        # parallel scan by loading CRAFT+CRNN models ahead of time.
        from app.config import OCR_PARALLEL_DUAL_PASS
        if OCR_PARALLEL_DUAL_PASS:
            import threading
            def _prewarm_reader2():
                try:
                    _ = hybrid.local_engine_2
                    logger.info("   ✅ Second OCR reader pre-warmed (parallel dual-pass ready)")
                except Exception as e:
                    logger.warning(f"   ⚠ Second OCR reader pre-warm failed: {e}")
            threading.Thread(target=_prewarm_reader2, daemon=True, name="ocr-reader2-prewarm").start()
            logger.info("   ⏳ Pre-warming second OCR reader in background...")

    if AZURE_DOC_INTEL_AVAILABLE:
        logger.info("   ✅ Azure Document Intelligence ready")

    logger.info("   ✅ Hybrid OCR engine ready")

    # ── OpenTelemetry Tracing ──
    from app.tracing import setup_tracing, shutdown_tracing
    setup_tracing(app)

    # ── Async Batch Processing Service ──
    from app.services.batch_service import get_batch_service
    _batch_svc = get_batch_service()
    await _batch_svc.start()
    logger.info("   ✅ Batch processing service ready")

    yield  # ← app runs here

    # ── SHUTDOWN ──
    await get_batch_service().shutdown()
    logger.info("   🛑 Batch processing service stopped")

    # Flush image cache to disk before shutdown — entries from the last 30s
    # debounce window would otherwise be lost.
    try:
        from app.ocr.image_cache import get_image_cache
        get_image_cache().flush()
        logger.info("   💾 Image cache flushed to disk")
    except Exception as e:
        logger.warning(f"   ⚠ Cache flush failed: {e}")

    shutdown_tracing()
    from app.database import db
    db.shutdown()
    logger.info(f"🛑 {APP_TITLE} shutting down...")


# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description=(
        "Scan handwritten shop receipts, extract item names and quantities "
        "using OCR, and generate structured Excel reports."
    ),
    docs_url="/docs" if API_DOCS_ENABLED else None,
    redoc_url="/redoc" if API_DOCS_ENABLED else None,
    lifespan=lifespan,
)

# ─── CORS Middleware ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)
# ─── Compression ──────────────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=500)  # Compress responses >500B (~60% savings on JSON/HTML)

# ─── Security Middleware ───────────────────────────────────────────────────
# Starlette middleware wraps app layers — LAST added = OUTERMOST (runs first).
# Order of execution: DevTunnelCORS → Security → RateLimit → APIKey → route.
# CORS must be outermost so 429/401 responses still include CORS headers,
# otherwise the browser shows a CORS error instead of the real error.
app.add_middleware(APIKeyMiddleware,              # Protect destructive endpoints
                   api_key=API_SECRET_KEY)
app.add_middleware(RateLimitMiddleware,           # Per-IP rate limiting
                   general_rpm=RATE_LIMIT_RPM,
                   scan_rpm=RATE_LIMIT_SCAN_RPM)
app.add_middleware(SecurityHeadersMiddleware)     # X-Frame-Options, CSP, etc.
app.add_middleware(DevTunnelCORSMiddleware)       # Dynamic CORS for VS Code tunnels (outermost)

# ─── Prometheus Metrics ────────────────────────────────────────────────────
try:
    from prometheus_fastapi_instrumentator import Instrumentator
    _instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/static/.*"],
    )
    _instrumentator.instrument(app)
    # Expose metrics at /internal/metrics (not /metrics) to reduce accidental exposure.
    # In production, restrict access via reverse proxy rules or firewall.
    _instrumentator.expose(app, endpoint="/internal/metrics", include_in_schema=False)
    logger.info("   📊 Prometheus metrics enabled at /internal/metrics")
except ImportError:
    logger.debug("   prometheus-fastapi-instrumentator not installed — metrics disabled")

# ─── Request Logging Middleware ────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every HTTP request with method, path, status, and duration."""
    start = time.time()
    logger.debug(f"→ {request.method} {request.url.path} (client={request.client.host if request.client else 'unknown'})")

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = int((time.time() - start) * 1000)
        logger.error(f"✗ {request.method} {request.url.path} | EXCEPTION in {elapsed}ms | {exc}")
        raise

    elapsed = int((time.time() - start) * 1000)
    level = logging.WARNING if response.status_code >= 400 else logging.DEBUG
    logger.log(
        level,
        f"← {request.method} {request.url.path} | {response.status_code} | {elapsed}ms",
    )
    return response

# ─── Static File Cache Headers ────────────────────────────────────────────────
@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    """Add Cache-Control headers for static assets (JS/CSS/images)."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        # 1 hour for JS/CSS; browsers still revalidate on hard refresh
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response

# ─── Static Files ─────────────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# NOTE: /uploads and /exports are NOT mounted as static directories.
# Files are served through /api/export/download/<filename> which validates
# the filename and restricts to .xlsx/.csv only — prevents directory browsing
# and arbitrary file exfiltration of uploaded receipt images.

# ─── API Routes ───────────────────────────────────────────────────────────────
app.include_router(router)

# ─── Training Pipeline Routes ─────────────────────────────────────────────────
from app.training.routes import training_router
app.include_router(training_router)


# ─── Serve Frontend ──────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_frontend():
    """Serve the main HTML page."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": f"{APP_TITLE} API v{APP_VERSION} is running. Visit /docs for API documentation."}


