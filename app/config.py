"""
Application configuration settings.
"""

import os
from pathlib import Path

# Load .env file if present (for Azure credentials, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed — use system env vars


# ─── Base Paths ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
MODEL_DIR = BASE_DIR / "models"

# SQLite doesn't work in OneDrive/cloud-synced folders (locking issues).
# Auto-redirect the database to a local folder if needed.
def _safe_db_path(base: Path) -> Path:
    """Avoid creating the DB inside cloud-synced folders (OneDrive, Dropbox, etc.).
    SQLite + file-sync = corruption. Use a local-only directory instead."""
    import sys
    base_str = str(base).lower()
    if "onedrive" in base_str or "dropbox" in base_str or "google drive" in base_str:
        if sys.platform == "win32":
            fallback = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "ReceiptScanner"
        else:
            # Linux/Mac: use ~/.local/share (XDG_DATA_HOME) or fallback to home
            fallback = Path(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))) / "ReceiptScanner"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "receipt_scanner.db"
    return base / "receipt_scanner.db"

DATABASE_PATH = _safe_db_path(BASE_DIR)

# ─── Database Backend ─────────────────────────────────────────────────────────
# "sqlite"      → local SQLite file  (default, zero-config)
# "postgresql"  → PostgreSQL server  (requires psycopg2-binary)
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite")

# Backup settings (SQLite only — daily copy before first write)
DB_BACKUP_DIR = BASE_DIR / "backups"
DB_BACKUP_KEEP_DAYS = int(os.getenv("DB_BACKUP_KEEP_DAYS", "7"))

# PostgreSQL connection (only used when DB_BACKEND = "postgresql")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "receipt_scanner")
POSTGRES_USER = os.getenv("POSTGRES_USER", "receipt_app")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_MIN_CONN = int(os.getenv("POSTGRES_MIN_CONN", "2"))
POSTGRES_MAX_CONN = int(os.getenv("POSTGRES_MAX_CONN", "10"))

# Create directories if they don't exist
UPLOAD_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)
DB_BACKUP_DIR.mkdir(exist_ok=True)


# ─── OCR Configuration ───────────────────────────────────────────────────────
OCR_LANGUAGE = "en"
OCR_USE_GPU = False  # Set to True if GPU is available
OCR_CONFIDENCE_THRESHOLD = 0.40  # Lower threshold for handwriting (was 0.85)
OCR_LOW_CONFIDENCE_THRESHOLD = 0.25  # Below this → flag entire receipt
OCR_TEXT_THRESHOLD = 0.4  # Lower to catch faint handwriting (was 0.7)
OCR_LOW_TEXT = 0.3  # Lower to catch faint text (was 0.4)
OCR_LINK_THRESHOLD = 0.3  # Link nearby characters (was 0.4)
OCR_CANVAS_SIZE = 1280  # Optimized: fast enough for same-type receipts, still captures handwriting
OCR_MAG_RATIO = 1.8  # Slightly lower mag trades marginal detail for ~15% speed gain
OCR_MIN_SIZE = 10  # Lower to catch small handwritten digits (single-digit quantities)

# Smart OCR pass strategy: run gray first (faster), only add color pass if
# the first pass yields fewer items than expected.  This cuts OCR time ~45%
# on typical receipts while preserving accuracy via fallback.
OCR_SMART_PASS_THRESHOLD = 3   # Low threshold: skip 2nd pass once 3+ items found (same-receipt-type optimization)
OCR_PARALLEL_DUAL_PASS = True  # Use ThreadPoolExecutor for dual-pass OCR


# ─── Azure Document Intelligence (Hybrid OCR) ────────────────────────────────
# Set these environment variables for cloud OCR (highest accuracy & speed).
# If not set, falls back to local EasyOCR automatically.
#   AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = "https://<your-resource>.cognitiveservices.azure.com/"
#   AZURE_DOCUMENT_INTELLIGENCE_KEY      = "<your-api-key>"
AZURE_DOC_INTEL_ENDPOINT = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "")
AZURE_DOC_INTEL_KEY = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "")
AZURE_DOC_INTEL_AVAILABLE = bool(AZURE_DOC_INTEL_ENDPOINT and AZURE_DOC_INTEL_KEY)

# ─── Hybrid OCR Engine Settings ──────────────────────────────────────────────
# OCR_ENGINE_MODE controls which engine(s) to use:
#   "auto"   → Local-first, Azure only when needed (RECOMMENDED — most cost-efficient)
#   "azure"  → Azure only (fails if Azure unavailable)
#   "local"  → EasyOCR only (original behavior, no cloud dependency)
OCR_ENGINE_MODE = os.getenv("OCR_ENGINE_MODE", "auto")

# Azure model selection strategy:
#   "receipt-only" → Use ONLY prebuilt-receipt ($0.01/page) — BEST for receipts (structured extraction)
#   "read-only"   → Use ONLY prebuilt-read ($0.0015/page) — cheapest, raw text only
#   "receipt-then-read" → Try receipt first, fall back to read (BURNS 2 PAGES if receipt fails!)
# NOTE: prebuilt-receipt handles BOTH printed AND handwritten receipts and natively
# extracts items, quantities, prices — dramatically better than raw text parsing.
AZURE_MODEL_STRATEGY = os.getenv("AZURE_MODEL_STRATEGY", "receipt-only")

# Azure receipt model confidence threshold — below this, re-run with Read model
# (Only used when AZURE_MODEL_STRATEGY = "receipt-then-read")
AZURE_RECEIPT_CONFIDENCE_THRESHOLD = 0.6

# Minimum items Azure receipt model should find before trusting it
AZURE_RECEIPT_MIN_ITEMS = 1

# Timeout for Azure API calls (seconds)
AZURE_API_TIMEOUT = 30

# Enable local fallback verification: after Azure extraction, optionally
# run a fast local pass to cross-verify results for critical accuracy
HYBRID_CROSS_VERIFY = False  # Set True for maximum accuracy at cost of speed

# ─── Image Quality Gate (skip Azure for bad images) ──────────────────────────
# If the image is too blurry or dark, don't waste an Azure page on it.
# These thresholds are checked before sending to Azure.
IMAGE_QUALITY_MIN_SHARPNESS = float(os.getenv("IMAGE_QUALITY_MIN_SHARPNESS", "30.0"))  # Laplacian variance
IMAGE_QUALITY_MIN_BRIGHTNESS = int(os.getenv("IMAGE_QUALITY_MIN_BRIGHTNESS", "40"))    # Mean pixel value (0-255)
IMAGE_QUALITY_GATE_ENABLED = os.getenv("IMAGE_QUALITY_GATE_ENABLED", "true").lower() == "true"


# ─── Azure Cost Control (CRITICAL — prevents surprise bills) ─────────────────
# Daily page limit: stop Azure calls after this many pages per day.
# Set conservatively to catch runaway usage (e.g., loops, retries).
AZURE_DAILY_PAGE_LIMIT = int(os.getenv("AZURE_DAILY_PAGE_LIMIT", "50"))

# Monthly page limit: hard cap on Azure pages per month.
# Azure free tier = 500 pages. Set this ≤ 500 to stay free forever.
AZURE_MONTHLY_PAGE_LIMIT = int(os.getenv("AZURE_MONTHLY_PAGE_LIMIT", "500"))

# Azure free tier pages (don't change unless Azure changes pricing)
AZURE_FREE_TIER_PAGES = 500

# Smart routing: if local OCR confidence > this threshold, skip Azure entirely.
# Lower = saves more Azure pages; Higher = uses Azure more often for accuracy.
# 0.85 ensures only genuinely well-read receipts bypass Azure. EasyOCR often
# reports 0.70-0.80 confidence on garbled handwritten text, so the old 0.72
# threshold was letting bad results through.
LOCAL_CONFIDENCE_SKIP_THRESHOLD = float(os.getenv("LOCAL_CONFIDENCE_SKIP_THRESHOLD", "0.85"))

# Minimum local OCR detections to trust local results (skip Azure).
# If local OCR finds >= this many text blocks, it probably read the receipt fine.
LOCAL_MIN_DETECTIONS_SKIP = int(os.getenv("LOCAL_MIN_DETECTIONS_SKIP", "4"))

# Minimum catalog match rate (0.0-1.0) for local OCR to skip Azure.
# Even with high confidence, if few detected words match known product codes,
# the local OCR probably misread the handwriting. Route to Azure for accuracy.
LOCAL_CATALOG_MATCH_SKIP_THRESHOLD = float(os.getenv("LOCAL_CATALOG_MATCH_SKIP_THRESHOLD", "0.3"))


# ─── Image Cache (prevents paying twice for same image) ──────────────────────
# Max cached OCR results (each ~2-5KB in memory)
IMAGE_CACHE_MAX_SIZE = int(os.getenv("IMAGE_CACHE_MAX_SIZE", "200"))

# Cache TTL: how long to reuse a cached result (seconds).
# 24 hours = same receipt re-scanned during the workday won't burn another Azure page.
IMAGE_CACHE_TTL = int(os.getenv("IMAGE_CACHE_TTL", "86400"))


# ─── Image Optimization (for Azure uploads) ──────────────────────────────────
# Max dimension for Azure uploads (Azure works fine at 1500px, larger is waste)
AZURE_IMAGE_MAX_DIMENSION = 1500

# JPEG quality for Azure uploads (lower = smaller file = faster upload)
AZURE_IMAGE_QUALITY = 85


# ─── Image Preprocessing ─────────────────────────────────────────────────────
IMAGE_MIN_WIDTH = 400
IMAGE_MIN_HEIGHT = 300
IMAGE_MAX_DIMENSION = 1800  # Larger to preserve handwriting detail for OCR accuracy
GAUSSIAN_BLUR_KERNEL = (3, 3)  # Gentler blur for handwriting (was 5,5)
ADAPTIVE_THRESH_BLOCK_SIZE = 31  # Larger block for handwriting (was 11)
ADAPTIVE_THRESH_C = 10  # Higher C preserves ink strokes (was 2)
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE_GRID_SIZE = (8, 8)


# ─── Fuzzy Matching ──────────────────────────────────────────────────────────
FUZZY_MATCH_CUTOFF = 0.72  # Default cutoff (overridden by adaptive logic for short codes)
FUZZY_MAX_RESULTS = 5  # More candidates (was 3)

def get_adaptive_fuzzy_cutoff(code_length: int) -> float:
    """Length-adaptive fuzzy match cutoff.
    Short codes need stricter matching to avoid false positives.
    Long codes can tolerate more OCR errors.
    
    Tightened for ≤4 chars to prevent single-char-difference false matches
    (e.g. TEW1→TEW4, MNO→MN0)."""
    if code_length <= 3:
        return 0.88  # Very strict for 3-char codes (e.g. ABC)
    elif code_length <= 4:
        return 0.82  # Strict for 4-char codes — 1 char diff = reject
    elif code_length <= 6:
        return 0.72  # Default for medium codes
    else:
        return 0.65  # Lenient for long codes (more chars = more OCR noise)


# ─── Excel Generation ────────────────────────────────────────────────────────
EXCEL_HEADER_COLOR = "4472C4"
EXCEL_HEADER_FONT_COLOR = "FFFFFF"
EXCEL_ALT_ROW_COLOR = "F2F2F2"
EXCEL_LOW_CONFIDENCE_COLOR = "FFD966"
EXCEL_MAX_COLUMN_WIDTH = 50


# ─── Application Settings ────────────────────────────────────────────────────
APP_TITLE = "Handwritten Receipt Scanner"
APP_VERSION = "1.0.0"
MAX_RECEIPTS_PER_BATCH = 50
MAX_FILE_SIZE_MB = 20
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
AUTO_SAVE_INTERVAL_SECONDS = 30


# ─── Logging Settings ─────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # DEBUG | INFO | WARNING | ERROR
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per log file
LOG_FILE_BACKUP_COUNT = 5  # Keep 5 rotated log files
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(funcName)-20s | L%(lineno)-4d | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


# ─── API Settings ────────────────────────────────────────────────────────────
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
API_DEBUG = os.getenv("API_DEBUG", "false").lower() in ("true", "1", "yes")
# API docs: disabled by default in production for security.
# Set API_DOCS_ENABLED=true in dev to re-enable /docs and /redoc.
API_DOCS_ENABLED = os.getenv("API_DOCS_ENABLED", "false").lower() in ("true", "1", "yes")

# CORS: restrict to known origins. Accepts comma-separated list via env var.
# Default allows localhost dev + VS Code Dev Tunnels.
_cors_env = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
    ]
)
# Dev Tunnels add origin at runtime — handled by middleware pattern match

# Rate Limiting (requests per minute per client IP)
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))       # general endpoints
RATE_LIMIT_SCAN_RPM = int(os.getenv("RATE_LIMIT_SCAN_RPM", "20"))  # scan + batch endpoints (expensive)

# API key for destructive operations (delete, reset, clear).
# In production, these endpoints require header: X-API-Key: <key>
# If not set and not in debug mode, a random key is auto-generated and logged.
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")
if not API_SECRET_KEY and not os.getenv("API_DEBUG", "false").lower() in ("true", "1", "yes"):
    import secrets as _secrets
    API_SECRET_KEY = _secrets.token_urlsafe(32)
    # SECURITY: Never print the actual key — it would leak to Docker logs,
    # CI/CD output, and log aggregators. Log a hint instead.
    import logging as _cfg_logging
    _cfg_logging.getLogger(__name__).warning(
        "API_SECRET_KEY not set — auto-generated a random key. "
        "Set API_SECRET_KEY env var in production to use a fixed key. "
        "Key starts with: %s...", API_SECRET_KEY[:8]
    )
