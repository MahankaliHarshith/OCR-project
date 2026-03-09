# Handwritten Receipt Scanner

<!-- LLM-CONTEXT: Python 3.11+ / FastAPI / EasyOCR + Azure Document Intelligence hybrid / OpenCV / SQLite WAL / Vanilla JS SPA / port 8000 -->

**Stack:** Python 3.11+, FastAPI + Uvicorn, EasyOCR (local), Azure Document Intelligence (cloud), OpenCV, SQLite (WAL), OpenPyXL, Vanilla JS SPA  
**Purpose:** Web app for small shops — scan handwritten receipts → OCR extract product codes + quantities → fuzzy-match to product catalog → save to DB → export formatted Excel reports.  
**Key constraint:** Operates within Azure free tier (500 pages/month) via a 6-layer cost defense system. Works fully offline with local EasyOCR alone.

---

## Quick Reference

```
Start:     python run.py                          → http://localhost:8000
API docs:  http://localhost:8000/docs              (Swagger UI)
Tests:     python -m pytest tests/
E2E:       python test_all_flows.py                (71 tests, embedded server on :8765)
Benchmark: python benchmark_pipeline.py
Debug:     python debug_qty.py / python dump_ocr.py
```

**Singletons:** `db`, `product_service`, `receipt_service`, `excel_service` = eager init at import.  
**Lazy singletons:** `get_ocr_engine()`, `get_azure_engine()`, `get_hybrid_engine()`, `get_usage_tracker()`, `get_image_cache()` — created on first call, cached globally.

---

## Project Structure (with line counts and responsibilities)

```
OCR project/
├── .env.example                  # All env var templates with comments
├── requirements.txt              # Pinned deps (==) for reproducible builds
├── run.py                        # Entry: uvicorn app.main:app (respects API_DEBUG env var)
├── receipt_scanner.db            # SQLite DB (auto-created, WAL mode)
│
├── app/
│   ├── config.py          (215L) # Central config — all env vars, API_DEBUG/API_HOST/API_PORT/API_DOCS_ENABLED
│   ├── main.py            (197L) # FastAPI app, lifespan handler (modern async context manager), middleware stack
│   ├── middleware.py      (245L) # 4 security middlewares + CSP header: DevTunnelCORS, SecurityHeaders, RateLimit, APIKey
│   ├── database.py        (950L) # ConnectionPool, MigrationManager, BackupManager, DatabaseBackend ABC, SQLite CRUD
│   ├── db_postgres.py     (350L) # PostgreSQL backend implementation (swap via DB_BACKEND=postgresql)
│   ├── logging_config.py  (110L) # RotatingFileHandler (10MB × 5) + console
│   │
│   ├── api/
│   │   └── routes.py      (575L) # REST endpoints. Scan uses asyncio.to_thread(). Magic-byte validation. Pagination.
│   │
│   ├── ocr/
│   │   ├── engine.py      (299L) # OCREngine: EasyOCR wrapper (extract_text / _fast / _turbo). JIT warmup.
│   │   ├── azure_engine.py(520L) # AzureOCREngine: prebuilt-read + prebuilt-receipt. Image optimization.
│   │   ├── hybrid_engine.py(872L)# HybridOCREngine: smart router — cache→quality→local→budget→Azure→fallback
│   │   ├── preprocessor.py(619L) # OpenCV pipeline: EXIF→resize→gray→deskew→enhance→CLAHE→crop. Grid detect.
│   │   ├── parser.py     (1330L) # Receipt text→items: 7 regex patterns, 4-tier code matching, qty extraction
│   │   ├── usage_tracker.py(395L)# Azure budget: daily/monthly limits, pacing, atomic JSON persistence
│   │   └── image_cache.py (240L) # SHA-256 → LRU OrderedDict. Disk-persisted. Thread-safe saves.
│   │
│   ├── services/
│   │   ├── receipt_service.py(520L) # 5-step orchestrator: save→preprocess→OCR→parse→DB
│   │   ├── product_service.py(200L) # Product CRUD + CSV import/export + fuzzy search + pagination
│   │   └── excel_service.py  (390L) # 2-sheet .xlsx with batch receipt fetch (no N+1)
│   │
│   └── static/
│       ├── index.html     (465L) # 3-tab SPA with ARIA roles, tabpanel, aria-live, form labels
│       ├── styles.css    (1830L) # CSS custom props, glassmorphic, skeleton loading, toast notifications
│       └── app.js        (1860L) # Client compress, camera, clipboard paste, keyboard shortcuts, visibility-aware polling
│
├── data/                         # Runtime data (auto-created)
│   ├── azure_usage.json          # Daily/monthly Azure page usage + cost estimates
│   └── image_cache.json          # Persisted OCR result cache (survives restarts)
├── uploads/                      # Receipt images (auto-cleaned >7 days at startup)
├── exports/                      # Generated Excel files (auto-cleaned >7 days at startup)
├── models/                       # EasyOCR models (~500MB, auto-downloaded on first run)
├── logs/                         # Rotating logs (10MB × 5 backups)
├── tests/
│   ├── test_app.py      (280L)   # 22 pytest tests (parser, Excel, DB)
│   └── test_db_production.py     # 46 tests (connection pool, migrations, backup, PostgreSQL)
└── docs/
    ├── HYBRID_OCR_ARCHITECTURE.md
    ├── AI_Receipt_Generation_Prompts.md
    └── Receipt_Design_and_Scanning_Guide.md
```

---

## System Architecture

### Component Dependency Graph

```
Browser (localhost:8000)
  │
  ▼
FastAPI + Middleware Stack
  │  DevTunnelCORS → SecurityHeaders → RateLimit → APIKey → CORSMiddleware → RequestLogging
  │
  ├─→ routes.py ──→ receipt_service.py (orchestrator)
  │                    ├─→ preprocessor.py (OpenCV pipeline)
  │                    ├─→ hybrid_engine.py (OCR router)
  │                    │     ├─→ image_cache.py (SHA-256 LRU)
  │                    │     ├─→ engine.py (local EasyOCR)
  │                    │     ├─→ azure_engine.py (cloud OCR)
  │                    │     └─→ usage_tracker.py (budget)
  │                    ├─→ parser.py (text → structured items)
  │                    └─→ database.py (SQLite)
  │
  ├─→ routes.py ──→ product_service.py ──→ database.py
  └─→ routes.py ──→ excel_service.py ──→ database.py
```

### Hybrid OCR Decision Pipeline (AUTO mode)

This is the exact decision flow in `hybrid_engine.py._run_auto_pipeline()`:

```
Step 0: image_cache.get(SHA-256 hash)
        → HIT: return cached result immediately (FREE, <1ms)
        → MISS: continue

Step 1: _check_image_quality(image)
        → sharpness < 30.0 (Laplacian variance) OR brightness < 40 (mean pixel):
          return local OCR only, skip Azure (strategy: "auto-quality-gate")
        → PASS: continue

Step 2: _run_local_pipeline(image)  [always runs, FREE]
        → engine.extract_text_fast() on grayscale
        → optional engine.extract_text() on EXIF-corrected color if first pass is weak
        → IF confidence >= 0.72 AND detections >= 4:
          return local result (strategy: "auto-local-skip")
        → ELSE: continue to Azure

Step 3: usage_tracker.can_call_azure()
        → daily_used >= 50 OR monthly_used >= 500:
          return local result (strategy: "auto-usage-limited")
        → ALLOWED: continue

Step 4: Azure API call (single model per AZURE_MODEL_STRATEGY)
        "read-only" (DEFAULT) → prebuilt-read      $0.0015/page
        "receipt-only"        → prebuilt-receipt     $0.01/page
        "receipt-then-read"   → receipt then read    up to 2 pages!
        → cache result via image_cache.put()
        → return (strategy: "auto-azure-read" or "auto-azure-receipt")

Step 5: Azure fails → return local result (strategy: "auto-fallback-local")
```

**Result:** ~40-60% of scans skip Azure entirely via Step 2, yielding ~800-1100 effective scans/month on the free 500-page tier.

---

## Module Deep-Dive

### `app/config.py` — Central Configuration

All settings loaded from `.env` via `load_dotenv()`. Key constants:

| Category | Constants | Values |
|----------|-----------|--------|
| **Paths** | `BASE_DIR`, `UPLOAD_DIR`, `EXPORT_DIR`, `MODEL_DIR`, `LOG_DIR`, `DATA_DIR` | All relative to project root, auto-created |
| **EasyOCR** | `CANVAS_SIZE`, `MAG_RATIO`, `CONFIDENCE_THRESHOLD`, `USE_GPU` | 1024, 2.0, 0.40, False |
| **Azure** | `AZURE_API_TIMEOUT`, `AZURE_IMAGE_MAX_DIMENSION`, `AZURE_IMAGE_QUALITY` | 30s, 1500px, 85 (JPEG quality) |
| **Preprocess** | `IMAGE_MAX_DIMENSION`, `CLAHE_CLIP_LIMIT`, `CLAHE_TILE_GRID` | 1280px, 2.0, (8,8) |
| **Fuzzy** | `FUZZY_MATCH_CUTOFF`, `FUZZY_MAX_RESULTS` | 0.5, 5 |
| **App** | `MAX_FILE_SIZE_MB`, `ALLOWED_EXTENSIONS` | 20MB, {jpg,jpeg,png,bmp,tiff,webp} |
| **Excel** | `EXCEL_HEADER_COLOR`, `ALT_ROW_COLOR`, `LOW_CONF_COLOR` | "4472C4", "F2F2F2", "FFD966" |

`AZURE_DOC_INTEL_AVAILABLE` is set at import time — credential changes require server restart.

### `app/ocr/engine.py` — Local EasyOCR Engine

| Method | Canvas | MagRatio | Speed | Use Case |
|--------|--------|----------|-------|----------|
| `extract_text()` | 1024 | 2.0 | ~3s | Full quality / color pass |
| `extract_text_fast()` | 960 | 1.2 | ~2s | Phase 1 fast pass (gray) |
| `extract_text_turbo()` | 640 | 1.0 | ~1.5s | Grid/structured receipts |

- JIT warmup on init: dummy 640×480 image eliminates 5-8s first-scan cold start.
- Returns: `[{bbox: [[x1,y1],[x2,y2],[x3,y3],[x4,y4]], text: str, confidence: float, needs_review: bool}]`

### `app/ocr/azure_engine.py` — Azure Document Intelligence

- `extract_receipt_structured(path)` → `{items, merchant, total, subtotal, tax, ocr_detections}`
- `extract_text_read(path)` → EasyOCR-compatible list (prebuilt-read)
- `extract_text_from_bytes(bytes, model)` → same from in-memory bytes
- `_optimize_image_for_upload()` → JPEG 1500px 85q (typically 4MB → ~300KB)
- Records usage via `usage_tracker` even on failure. Polygon bboxes in clockwise TL,TR,BR,BL order.

### `app/ocr/usage_tracker.py` — Azure Budget Pacing

`can_call_azure()` returns:
```python
{
    "allowed": bool,
    "reason": str,
    "daily_used": int, "daily_remaining": int,
    "monthly_used": int, "monthly_remaining": int,
    "pace_status": "ok" | "fast" | "critical",  # critical = daily > sustainable × 1.5
    "sustainable_daily_rate": float,              # 500 / days_remaining_in_month
    "days_left_in_month": int,
    "estimated_cost": float,
    "is_within_free_tier": bool
}
```

Persistence: `data/azure_usage.json` — atomic write (tempfile → rename). Daily entries auto-pruned after 7 days. Free tier math: 500 pages/month ÷ ~22 work days ≈ 22 pages/day sustainable rate.

### `app/ocr/image_cache.py` — Result Cache

- Key: SHA-256 of raw file bytes. Value: full OCR result dict.
- LRU OrderedDict, max 200 entries, 24h TTL.
- Disk-persisted to `data/image_cache.json` — survives server restarts.
- Thread-safe: `_save_to_disk_unlocked()` runs inside `threading.Lock`.
- `get_stats()` → `{size, hit_rate, azure_calls_saved, persisted: bool}`

### `app/ocr/parser.py` — Receipt Text Parser

**7 regex patterns (priority order):**
1. `CODE - QTY` 2. `CODE QTY` 3. `QTY CODE` 4. `CODE x QTY` 5. `CODE: QTY` 6. `CODE(QTY)` 7. `^CODE$` (qty=1)

**4-tier code matching:** exact → OCR char substitutions (`|`→I, `0`→O, `6`→G, etc.) → handwriting substitutions (`n`→H, `l`→I, etc.) → fuzzy (difflib cutoff 0.5)

**QT-marker recognition:** `Q1`, `QI`, `qt`, `&T`, `4T` all recognized as quantity suffix patterns.

**Pipeline:** group lines by Y-coordinate (threshold = max(50, 5% of image height)) → apply skip patterns → clean text → match → orphan quantity association → aggregate duplicates (sum quantities).

**Output shape:**
```python
{
    "receipt_id": "REC-20260303-143022-a1b2c",  # includes UUID suffix to prevent collisions
    "items": [{"code": str, "product": str, "quantity": int, "unit": str,
               "confidence": float, "needs_review": bool, "match_type": str, "raw_text": str}],
    "total_items": int, "avg_confidence": float, "unparsed_lines": [str]
}
```

### `app/ocr/preprocessor.py` — OpenCV Image Pipeline

```
Raw image → _load_with_exif_correction() → resize (max 1280px)
→ grayscale → deskew (HoughLinesP, ±15°, skip if std>5°)
→ quality check (Laplacian variance + mean brightness)
→ enhance: Gaussian blur(3,3) + unsharp if blurry + bilateral if low quality
→ morphological close (2×2) + CLAHE(2.0, 8×8) + brightness normalize
→ perspective correct (skip if no quad ≥300×300 or aspect change >30%)
→ crop_to_content() (Otsu → nonzero bbox + 5% margin, min 300×300)
```

`detect_grid_structure()` → True if ≥6 horizontal lines AND ≥3 vertical lines → routes to `extract_text_turbo()`.

### `app/services/receipt_service.py` — 5-Step Pipeline

1. `_save_uploaded_image()` → `uploads/receipt_YYYYMMDD_HHMMSS_<uuid6>.ext`
2. `preprocessor.preprocess()` → saves processed image alongside original
3. `detect_grid_structure()` → `hybrid_engine.process_image(path, processed, is_structured)`
4. If Azure returned structured items → `_parse_azure_structured()` (4-tier: exact→contains→fuzzy→unmatched). Else → `parser.parse(ocr_detections)`. If Azure returns < 2 items → supplements with parser.
5. `db.create_receipt()` + `add_receipt_items()` + 4× `add_processing_log()`. DB failure sets `success: false`.

### `app/database.py` — SQLite with Production Infrastructure

**Architecture:**
- `ConnectionPool` — thread-local SQLite connections with WAL mode
- `MigrationManager` — versioned schema migrations (forward-only, tracks applied versions)
- `BackupManager` — automated daily backups with configurable retention
- `DatabaseBackend` (ABC) — swappable backend interface; `Database` = SQLite impl
- `db_postgres.py` — PostgreSQL implementation (activate via `DB_BACKEND=postgresql`)

```sql
products:        id, product_code(UNIQUE), product_name, category, unit, is_active (soft-delete)
receipts:        id, receipt_number(UNIQUE), scan_date, scan_time, image_path, processed_image_path,
                 processing_status, total_items, ocr_confidence_avg
receipt_items:   id, receipt_id(FK CASCADE), product_code, product_name, quantity, unit,
                 ocr_confidence, manually_edited
processing_logs: id, receipt_id(FK), stage, status, duration_ms, error_message, timestamp
```

**Key methods:** `get_all_products(limit, offset)`, `count_products()`, `get_recent_receipts(limit, offset)`, `count_receipts()`, `get_receipts_batch(ids)` (2-query batch fetch for Excel export — no N+1).

Key behaviors: soft-delete reactivates on re-add; `add_receipt_item()` verifies receipt exists (FK check), sets confidence=1.0 + manually_edited=1 for manual rows; `update_receipt_item()` returns bool via `cursor.rowcount`.

**Seed data (10 products):** ABC(1L Exterior Paint), XYZ(1L Interior Paint), PQR(5L Primer), MNO(Paint Brush), DEF(1L Wood Varnish), GHI(Sandpaper), JKL(Putty Knife), STU(Wall Filler), VWX(Masking Tape), RST(Thinner 500ml).

---

## REST API Reference

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/health` | — | `{version, ocr_mode, azure_available, local_loaded}` |
| `GET` | `/api/dashboard` | — | Full stats + `ocr_engine: get_engine_status()` |
| `POST` | `/api/receipts/scan` | — | Multipart upload, 1MB chunk stream, UUID filename, magic-byte validation, `asyncio.to_thread()` |
| `GET` | `/api/receipts` | — | `?limit=20&offset=0` (paginated, returns `total`) |
| `GET` | `/api/receipts/{id}` | — | Receipt + items |
| `GET` | `/api/receipts/date/{YYYY-MM-DD}` | — | Receipts for date |
| `PUT` | `/api/receipts/items/{id}` | — | Pydantic `ItemUpdate` (code, name, qty validated) |
| `POST` | `/api/receipts/{id}/items` | — | Manual row add, 404 if receipt not found |
| `DELETE` | `/api/receipts/{id}` | 🔒 | API key required if `API_SECRET_KEY` is set |
| `GET/POST/PUT/DELETE` | `/api/products[/{code}]` | 🔒 DELETE | Full CRUD, soft-delete |
| `GET` | `/api/products` | — | `?limit=0&offset=0` (paginated, `limit=0` = all, returns `total`) |
| `GET` | `/api/products/search?q=` | — | LIKE wildcard-escaped search |
| `GET` | `/api/products/export/csv` | — | CSV download |
| `POST` | `/api/products/import/csv` | — | CSV upload |
| `POST` | `/api/export/excel` | — | `{receipt_ids: [int]}` → .xlsx (batch DB fetch, no N+1) |
| `GET` | `/api/export/daily?date=YYYY-MM-DD` | — | Daily summary Excel |
| `GET` | `/api/export/download/{filename}` | — | Path traversal guard, .xlsx/.csv only |
| `GET` | `/uploads/{filename}` | — | Secure image serving (path traversal guard, image-ext whitelist) |
| `GET` | `/api/ocr/status` | — | Engine mode, Azure available, cache stats |
| `GET` | `/api/ocr/usage` | — | Usage + cache stats + pacing dict |
| `POST` | `/api/ocr/usage/reset-daily` | 🔒 | Reset today's Azure counter |
| `POST` | `/api/ocr/cache/clear` | 🔒 | Wipe image cache (memory + disk) |

**Pydantic validators:** `product_code` → strip + uppercase + `^[A-Z0-9_\-]{1,10}$`; `quantity` → 0 < qty ≤ 99999; names strip `<>{}\\`. Response JSON uses `NumpyEncoder` for np.integer/floating/bool_/ndarray.

---

## Security Middleware Stack (`middleware.py`)

Registered in `main.py` — outermost first:

| Order | Middleware | Behavior |
|-------|-----------|----------|
| 1 | `DevTunnelCORSMiddleware` | Allows `*.devtunnels.ms` + `*.github.dev` dynamically. Uses `urlparse().hostname.endswith()` (not string contains). `Access-Control-Max-Age: 600` |
| 2 | `SecurityHeadersMiddleware` | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, **`Content-Security-Policy`** (default-src 'self', script/style 'unsafe-inline', Google Fonts, blob/data for images) |
| 3 | `RateLimitMiddleware` | Sliding 60s window per IP. Scan endpoints: 10 RPM. Others: 30 RPM. Returns `429 + Retry-After: 60`. Thread-safe via `threading.Lock`. |
| 4 | `APIKeyMiddleware` | Guards DELETE + reset-daily + cache-clear. Skipped if `API_SECRET_KEY=""`. Header: `X-API-Key` |
| 5 | `CORSMiddleware` (Starlette) | Standard CORS for configured origins |
| 6 | Request Logging | Logs method, path, status, duration |

---

## Environment Variables (`.env` / `app/config.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_DEBUG` | `false` | Set `true` to enable auto-reload + debug logging |
| `API_HOST` | `0.0.0.0` | Uvicorn bind host |
| `API_PORT` | `8000` | Uvicorn bind port |
| `API_DOCS_ENABLED` | `true` | Set `false` to hide /docs and /redoc in production |
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgresql` (requires db_postgres.py) |
| `OCR_ENGINE_MODE` | `auto` | `auto` (hybrid) / `azure` (cloud-only) / `local` (EasyOCR-only) |
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | `""` | Azure resource URL. Activates Azure when set. |
| `AZURE_DOCUMENT_INTELLIGENCE_KEY` | `""` | Azure API key |
| `AZURE_MODEL_STRATEGY` | `read-only` | `read-only` ($0.0015/pg) · `receipt-only` ($0.01/pg) · `receipt-then-read` (up to 2 pages!) |
| `AZURE_DAILY_PAGE_LIMIT` | `50` | Hard stop per day; falls back to local |
| `AZURE_MONTHLY_PAGE_LIMIT` | `500` | Hard stop per month (= free tier) |
| `LOCAL_CONFIDENCE_SKIP_THRESHOLD` | `0.72` | Skip Azure if local conf ≥ this |
| `LOCAL_MIN_DETECTIONS_SKIP` | `4` | Both conf AND detections must pass to skip Azure |
| `IMAGE_QUALITY_GATE_ENABLED` | `true` | Reject blurry/dark images from Azure |
| `IMAGE_QUALITY_MIN_SHARPNESS` | `30.0` | Laplacian variance threshold |
| `IMAGE_QUALITY_MIN_BRIGHTNESS` | `40` | Mean pixel value threshold |
| `IMAGE_CACHE_MAX_SIZE` | `200` | LRU cache entries |
| `IMAGE_CACHE_TTL` | `86400` | Cache TTL in seconds (24 hours) |
| `RATE_LIMIT_RPM` | `30` | Per-IP general rate limit (requests/min) |
| `RATE_LIMIT_SCAN_RPM` | `10` | Per-IP scan rate limit (requests/min) |
| `API_SECRET_KEY` | `""` | Protects DELETE/reset/clear endpoints if set |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `CORS_ORIGINS` | `localhost` | Comma-separated additional origins |
| `NGROK_AUTH_TOKEN` | `""` | For `start_public.py` (ngrok tunnel) |
| `HYBRID_CROSS_VERIFY` | `False` | If True, runs local after Azure for cross-verification (doubles cost) |

`config.py` calls `load_dotenv(BASE_DIR / ".env")` at import time (silent if python-dotenv missing).

---

## Startup Lifecycle (`main.py`)

1. `setup_logging()` — create log dirs, configure rotating file + console handlers
2. Define `lifespan()` async context manager (modern replacement for deprecated `on_event`)
3. Create `FastAPI(lifespan=lifespan)` with conditional docs (`API_DOCS_ENABLED`)
4. Mount static files: `/static` only (uploads/exports served via authenticated routes)
5. Register all 6 middleware layers (order matters — see Security section)
6. **Lifespan startup:**
   - Clean `uploads/` AND `exports/` — delete files older than 7 days
   - Pre-init `get_hybrid_engine()` (creates usage_tracker + image_cache)
   - If mode ≠ `azure` → pre-load `get_ocr_engine()` (pays EasyOCR cold start at launch, not first scan)
   - Log: server URL, docs URL, log file path
7. **Lifespan shutdown:**
   - `db.shutdown()` — closes connection pool
   - Log shutdown message

---

## Frontend (`app/static/`)

**3-tab SPA:** Scan | Receipts | Products. No build step, no framework.

Key behaviors in `app.js`:
- **Client-side compression:** images >1800px resized → JPEG quality 0.88 before upload
- **Camera integration:** `getUserMedia()` → `<video>` viewfinder → `canvas.toBlob()` → `processFile()`
- **Clipboard paste:** `document.addEventListener('paste')` (images only, scan tab active)
- **Keyboard shortcuts:** `1/2/3` = tabs, `N` = new scan, `C` = camera, `Escape` = close modal
- **Auto-fill:** typed product code → lookup `catalogCache` → auto-fill product name (green tint)
- **Dashboard refresh:** every 30 seconds; **pauses when tab is hidden** (visibility API); `perfState.processingTimes` keeps last 20 entries
- **Batch mode:** toggle to scan multiple receipts → "Export Batch" downloads single combined Excel
- **Beforeunload warning:** prompts if unsaved scan results exist
- **ARIA / Accessibility:** `role="tablist"` + `role="tab"` + `aria-selected` on nav; `role="tabpanel"` + `aria-labelledby` on sections; `aria-live="polite"` on processing/results; `aria-live="assertive"` on toasts; `role="dialog"` + `aria-modal` on modal; `for=` on form labels; `aria-label` on inputs; `aria-hidden="true"` on decorative icons

---

## How to Use

### Setup

```bash
cd "OCR project"
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

First run downloads ~500MB EasyOCR models (automatic, one-time).

### Azure Setup (optional — improves accuracy on difficult handwriting)

1. [Azure Portal](https://portal.azure.com) → Create **Document Intelligence** resource → **Free tier (F0)**
2. Copy **Endpoint** + **Key 1** from "Keys and Endpoint" blade
3. Create `.env` in project root:
   ```
   AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
   AZURE_DOCUMENT_INTELLIGENCE_KEY=your-key-here
   ```
4. Restart server — auto-detects Azure and switches to hybrid mode

### Scanning Workflow

1. Open `http://localhost:8000` → **Scan tab**
2. Upload image (drag & drop, file picker, camera, or clipboard paste)
3. Wait 2-5 seconds — preprocessing → OCR → parsing runs automatically
4. Review results — color-coded confidence: 🟢 ≥70% | 🟡 40-70% | 🔴 <40%
5. Click cells to edit product code/name/quantity if needed
6. Confirm to save to database

### Batch Export

Scan multiple receipts → click "View Batch" → "Export Batch" → single Excel with all receipts + summary sheet.

### Product Catalog

Products tab → Add manually or **Import CSV** (format: `code,name,category,unit`). Export as CSV backup. Fuzzy search by code or name.

---

## Known Behaviors & Edge Cases

> These are important for any LLM or developer modifying this codebase.

- `AZURE_DOC_INTEL_AVAILABLE` is set at **import time** — changing `.env` credentials requires full server restart.
- `receipt-then-read` strategy consumes **2 Azure pages per scan** — avoid as default.
- Budget pacing (`pace_status: "fast"/"critical"`) **warns** but does **not** block — only `daily/monthly_limit` is a hard stop.
- `data/` directory is lazy-created on first write; `image_cache.json` starts fresh if corrupted (silent recovery).
- Receipt numbers: `REC-YYYYMMDD-HHMMSS-<uuid5>` — UUID suffix prevents same-second collision.
- `HYBRID_CROSS_VERIFY=True` doubles Azure cost — only enable for accuracy benchmarking.
- Images saved 3× per scan: raw upload + copy in uploads/ + processed version. Both `uploads/` and `exports/` cleaned at startup (>7 days).
- `/uploads` and `/exports` are **not** mounted as static directories — files served through authenticated routes with path traversal protection.
- Upload files receive **magic-byte validation** — only real JPEG/PNG/BMP/TIFF/WebP accepted (blocks renamed files).
- Orphaned upload files are **cleaned up** on scan failure (`unlink` in except block).
- SQL LIKE search **escapes** `%` and `_` wildcards in user input to prevent injection.
- `API_DEBUG` defaults to `false` — set `true` in `.env` to enable reload mode.
- `API_DOCS_ENABLED` defaults to `true` — set `false` to hide Swagger/ReDoc in production.
- `LOG_LEVEL` defaults to `INFO` — set `DEBUG` for development.
- `AUTO_SAVE_INTERVAL_SECONDS` (30s) and `MAX_RECEIPTS_PER_BATCH` (50) are defined in config but **not implemented** in code.
- EasyOCR `USE_GPU=False` — CPU-only by default. Set to True + install CUDA PyTorch for GPU acceleration.
- SQLite `processing_logs` FK has **no CASCADE** — deleted manually when receipt is deleted.
- `NumpyEncoder` in routes.py handles numpy types in JSON responses (np.integer, np.floating, np.bool_, np.ndarray).
- Dependencies are **pinned to exact versions** (`==`) in `requirements.txt` for reproducible builds.

---

## Tests

### Unit Tests (`tests/test_app.py`) — 22 tests
Parser, Excel service, and database integration tests. Run with:
```bash
pytest tests/test_app.py -v
```

### Database Production Tests (`tests/test_db_production.py`) — 46 tests
Covers 9 categories: ConnectionPool, MigrationManager, BackupManager, DatabaseBackend ABC, PostgreSQL adapter, pagination, batch queries, soft-delete, and concurrent access.
```bash
python tests/test_db_production.py
```

### Verification Script (`tests/verify_db.py`)
Quick smoke test for production database features.

---

## Production Enhancements Changelog

### Speed & Accuracy (Batches 1–5, 19 improvements)
- Parallel dual-pass OCR, early cache checks, confidence-gated cache writes
- Shadow normalization, cross-line quantity look-ahead, post-parse quantity sanity checks
- Y-aware dedup, thread-safe dual engine, N+1 query fix
- Catalog refresh TTL, static file cache headers, `executemany` batch inserts
- Product-not-found LRU cache, color image pipeline passthrough
- Batch processing log inserts, dual-pass confidence boost
- Dashboard parallelization, tighter ROI crop threshold

### Database Production Readiness
- `ConnectionPool` — thread-local connections with configurable pool size
- `MigrationManager` — versioned forward-only schema migrations
- `BackupManager` — automated daily SQLite backups with retention policy
- `DatabaseBackend` ABC — swappable backend interface
- `db_postgres.py` — complete PostgreSQL implementation

### Production Hardening (10 fixes)
1. `API_DEBUG` defaults false; `API_HOST`, `API_PORT`, `API_DOCS_ENABLED` env vars
2. Modern `lifespan` async context manager (replaced deprecated `on_event`)
3. `Content-Security-Policy` header in security middleware
4. Conditional Swagger/ReDoc docs (hide in production)
5. Removed `/uploads` and `/exports` static mounts → secure authenticated routes
6. Magic-byte validation on file uploads (JPEG/PNG/BMP/TIFF/WebP signatures)
7. Orphan file cleanup on scan failure
8. SQL LIKE wildcard escaping in search
9. Dashboard polling pauses when browser tab is hidden
10. `LOG_LEVEL` default changed from DEBUG to INFO

### Quick Wins (Batch 7)
1. **Pinned dependencies** — all `==` exact versions for reproducible builds
2. **Pagination** — `GET /api/products?limit=&offset=` and `GET /api/receipts?limit=&offset=` with total counts
3. **N+1 fix** — Excel export uses batch `get_receipts_batch()` (2 queries instead of N+1)
4. **ARIA / Accessibility** — full tablist/tab/tabpanel roles, aria-live regions, aria-modal, form labels, aria-hidden on decorative icons

---

## License

MIT License
