# Project Context — Handwritten Receipt Scanner

**Stack:** Python 3.11+, FastAPI, EasyOCR, Azure Document Intelligence, OpenCV, SQLite (WAL) / PostgreSQL, Vanilla JS SPA  
**Purpose:** Local web app for small shops to scan handwritten receipts → extract product codes + quantities → save to DB → export Excel.  
**Key constraint:** Operates within Azure free tier (500 pages/month) via 6-layer cost defense. Works fully offline with local EasyOCR alone.

---

## Directory Layout

```
OCR project/
├── run.py / start_server.py / start_public.py   # Server launchers (direct / subprocess / ngrok)
├── requirements.txt / .env.example / README.md / CONTEXT.md
├── benchmark_pipeline.py                         # Times each pipeline stage
├── debug_qty.py / dump_ocr.py                    # Debug/diagnostic utilities
├── test_all_flows.py (465L)                      # E2E API tests with embedded server on :8765
├── test_comprehensive.py / test_boxed_template.py / test_qty.py / test_qty2.py / test_sample_inputs.py
├── docs/
│   ├── HYBRID_OCR_ARCHITECTURE.md
│   ├── AI_Receipt_Generation_Prompts.md
│   └── Receipt_Design_and_Scanning_Guide.md
├── app/
│   ├── config.py (222L)         main.py (196L)   middleware.py (244L)
│   ├── database.py (969L)       db_postgres.py (506L)   logging_config.py (109L)
│   ├── api/routes.py (576L)
│   ├── ocr/
│   │   ├── preprocessor.py (659L)   engine.py (299L)     parser.py (~1640L)
│   │   ├── azure_engine.py (520L)   hybrid_engine.py (994L)
│   │   ├── usage_tracker.py (414L)  image_cache.py (241L)
│   └── services/
│       ├── receipt_service.py (517L)   product_service.py (197L)   excel_service.py (386L)
│   └── static/  index.html (459L)  styles.css (1833L)  app.js (1861L)
├── uploads/ exports/ models/ logs/ data/ backups/
│   data/azure_usage.json  data/image_cache.json   # runtime, auto-created
│   backups/receipt_scanner_YYYY-MM-DD.db           # daily SQLite backup, auto-pruned
└── receipt_scanner.db  tests/test_app.py (243L)
```

---

## Environment Variables (all tunable via `.env`)

| Variable | Default | Notes |
|---|---|---|
| `OCR_ENGINE_MODE` | `auto` | `auto` / `azure` / `local` |
| `AZURE_MODEL_STRATEGY` | `read-only` | `read-only`($0.0015/pg) · `receipt-only`($0.01/pg) · `receipt-then-read`(may use 2 pages!) |
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | `""` | Activates Azure when set |
| `AZURE_DOCUMENT_INTELLIGENCE_KEY` | `""` | |
| `AZURE_DAILY_PAGE_LIMIT` | `50` | Hard stop; falls back to local |
| `AZURE_MONTHLY_PAGE_LIMIT` | `500` | = free tier |
| `LOCAL_CONFIDENCE_SKIP_THRESHOLD` | `0.72` | Skip Azure if local conf ≥ this |
| `LOCAL_MIN_DETECTIONS_SKIP` | `4` | Both conf AND detections must pass to skip Azure |
| `IMAGE_QUALITY_GATE_ENABLED` | `true` | Reject blurry/dark images from Azure |
| `IMAGE_QUALITY_MIN_SHARPNESS` | `30.0` | Laplacian variance threshold |
| `IMAGE_QUALITY_MIN_BRIGHTNESS` | `40` | Mean pixel value threshold |
| `IMAGE_CACHE_MAX_SIZE` | `200` | LRU entries |
| `IMAGE_CACHE_TTL` | `86400` | Seconds (24h) |
| `RATE_LIMIT_RPM` | `30` | Per-IP general rate limit |
| `RATE_LIMIT_SCAN_RPM` | `10` | Per-IP scan rate limit |
| `API_SECRET_KEY` | `""` | Protects DELETE/reset/clear endpoints if set |
| `API_DOCS_ENABLED` | `true` | Show /docs and /redoc Swagger UI |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `CORS_ORIGINS` | localhost | Comma-separated extra origins |
| `NGROK_AUTH_TOKEN` | `""` | For `start_public.py` |
| `DB_BACKEND` | `sqlite` | `sqlite` / `postgresql` |
| `DB_BACKUP_KEEP_DAYS` | `7` | Days to keep daily SQLite backups |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host (only when DB_BACKEND=postgresql) |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `receipt_scanner` | PostgreSQL database name |
| `POSTGRES_USER` | `receipt_app` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `""` | PostgreSQL password |
| `POSTGRES_MIN_CONN` | `2` | Min connections in PG pool |
| `POSTGRES_MAX_CONN` | `10` | Max connections in PG pool |

**dotenv:** `config.py` calls `load_dotenv(BASE_DIR / ".env")` at import (silent if python-dotenv missing).

---

## Key Config Constants (`app/config.py`)

```
Paths:     BASE_DIR / uploads / exports / models / logs / data / backups (all auto-created)
EasyOCR:   CANVAS_SIZE=1536, MAG_RATIO=2.0, MIN_SIZE=10, CONFIDENCE_THRESHOLD=0.40, USE_GPU=False
Azure:     AZURE_API_TIMEOUT=30, AZURE_IMAGE_MAX_DIMENSION=1500, AZURE_IMAGE_QUALITY=85
           AZURE_RECEIPT_CONFIDENCE_THRESHOLD=0.6, AZURE_RECEIPT_MIN_ITEMS=1
           HYBRID_CROSS_VERIFY=False  (True = always run local after Azure → doubles cost)
Preprocess: IMAGE_MAX_DIMENSION=1800, CLAHE_CLIP_LIMIT=2.0, CLAHE_TILE_GRID=(8,8)
Excel:     EXCEL_HEADER_COLOR="4472C4", ALT_ROW_COLOR="F2F2F2", LOW_CONF_COLOR="FFD966"
Fuzzy:     FUZZY_MATCH_CUTOFF=0.6, FUZZY_MAX_RESULTS=5
App:       MAX_FILE_SIZE_MB=20, ALLOWED_EXTENSIONS={jpg,jpeg,png,bmp,tiff,webp}
           API_DOCS_ENABLED=True (controls /docs and /redoc endpoints)
```

---

## Database Architecture (`database.py` — 969L)

### Architecture (6 subsystems)

```
┌─ ConnectionPool ────────────────────────────────────────────┐
│  Thread-local SQLite connections (threading.local())         │
│  One conn per thread, liveness-checked, auto-reconnect      │
│  close_all() at shutdown                                    │
└─────────────────────────────────────────────────────────────┘
         ↓
┌─ BackupManager ─────────────────────────────────────────────┐
│  Daily snapshot of DB file before first write of each day    │
│  Stored in backups/receipt_scanner_YYYY-MM-DD.db             │
│  Auto-prune old backups beyond DB_BACKUP_KEEP_DAYS (7)       │
│  Thread-safe (lock), no-op if already backed up today        │
└─────────────────────────────────────────────────────────────┘
         ↓
┌─ MigrationManager ──────────────────────────────────────────┐
│  Versioned schema migrations with audit trail                │
│  schema_migrations table: version, name, applied_at          │
│  v1 = baseline schema, v2 = composite index                  │
│  Auto-detects pre-migration DB (tables exist but no version) │
│  How to add: write _migration_vN_xxx() → append to MIGRATIONS│
└─────────────────────────────────────────────────────────────┘
         ↓
┌─ DatabaseBackend (ABC) ─────────────────────────────────────┐
│  Abstract interface — all services depend only on this       │
│  Methods: get_all_products, create_receipt, add_receipt_items │
│  get_receipts_batch, add_processing_logs_batch, shutdown, ...│
└────────────┬─────────────────────────┬──────────────────────┘
             ↓                         ↓
┌─ Database (SQLite) ──┐   ┌─ PostgreSQLDatabase ─────────────┐
│  WAL mode, conn pool  │   │  psycopg2 ThreadedConnectionPool │
│  daily backup         │   │  Drop-in replacement             │
│  schema migrations    │   │  DB_BACKEND=postgresql to switch  │
│  seed data on init    │   │  Same data shapes, zero code chg │
└───────────────────────┘   └──────────────────────────────────┘
             ↓
┌─ get_database() factory + singleton ────────────────────────┐
│  Returns Database() or PostgreSQLDatabase() based on config  │
│  `db: DatabaseBackend = get_database()` — singleton at import│
└─────────────────────────────────────────────────────────────┘
```

### Schema

```sql
products:          id, product_code(UNIQUE), product_name, category, unit, is_active, created_at, updated_at
receipts:          id, receipt_number(UNIQUE), scan_date, scan_time, image_path, processed_image_path,
                   processing_status, total_items, ocr_confidence_avg, created_at
receipt_items:     id, receipt_id(FK CASCADE), product_code, product_name, quantity, unit,
                   ocr_confidence, manually_edited
processing_logs:   id, receipt_id(FK no-cascade), stage, status, duration_ms, error_message, timestamp
schema_migrations: version(PK), name, applied_at   ← NEW migration tracking
```

### Key Behaviors

- **Thread-local pool:** each thread reuses one SQLite connection (10-50× fewer file-handle ops)
- **_before_write():** every mutating method calls this → triggers daily backup before first write
- **Soft-delete** reactivates on re-add; `add_receipt_item()` verifies receipt exists (FK check), sets confidence=1.0 + manually_edited=1
- **update_receipt_item()** returns `bool` via `cursor.rowcount` (0 = not found)
- **get_receipts_by_date()** and **get_receipts_batch()** use batched `IN(...)` query for items (N+1 fix)
- **add_processing_logs_batch()** — single `executemany` for all 4 logs per scan
- **count_products()** / **count_receipts()** — dedicated count queries
- **get_all_products(limit, offset)** — pagination support
- **processing_logs FK has no CASCADE** — deleted manually in delete_receipt()
- **shutdown()** closes all pooled connections (called from lifespan handler)

### Seed Data (10 products)
ABC(1L Exterior Paint), XYZ(1L Interior Paint), PQR(5L Primer), MNO(Paint Brush), DEF(1L Wood Varnish), GHI(Sandpaper), JKL(Putty Knife), STU(Wall Filler), VWX(Masking Tape), RST(Thinner 500ml)

---

## PostgreSQL Backend (`db_postgres.py` — 506L)

- Drop-in replacement: set `DB_BACKEND=postgresql` + `POSTGRES_*` env vars
- Uses `psycopg2.pool.ThreadedConnectionPool` (min 2, max 10 connections)
- `RealDictCursor` for dict-like row access
- All methods return identical data shapes to SQLite — zero service/route changes needed
- Auto-creates tables on init (same schema as SQLite, adapted for PG syntax)
- Requires: `pip install psycopg2-binary`

---

## Hybrid OCR Pipeline (AUTO mode — `hybrid_engine.py`)

```
[0] image_cache.get(SHA-256)          → HIT: return free          (strategy: auto-cached)
[1] _check_image_quality()            → sharpness<30 OR dark<40:
                                         return local only         (auto-quality-gate)
[2] _run_local_pipeline() (free)
    crop → turbo/fast → count items
    → optional color EXIF-corrected pass
    conf≥0.72 AND detections≥4?       → return local              (auto-local-skip)
[3] usage_tracker.can_call_azure()    → blocked?  return local     (auto-usage-limited)
[4] Azure — single model per AZURE_MODEL_STRATEGY
    "read-only"        → prebuilt-read     $0.0015   ← DEFAULT
    "receipt-only"     → prebuilt-receipt  $0.01
    "receipt-then-read"→ receipt then read (up to 2 pages!)
    → image_cache.put() → return                                   (auto-azure-read/receipt)
[5] Azure failed → return local                                    (auto-fallback-local)
[Optional] HYBRID_CROSS_VERIFY=True → run local again to cross-check (1.15× boost on overlap)
```

**`_check_image_quality()` returns:** `{acceptable, sharpness (Laplacian var), brightness (mean px), reason}`

---

## Preprocessing Pipeline (`preprocessor.py`)

```
Raw image → _load_with_exif_correction() → resize (max 1280px)
→ grayscale → deskew (HoughLinesP, ±15°, skip if std>5°)
→ quality check (Laplacian + brightness)
→ enhance: Gaussian blur(3,3) + unsharp if blurry + bilateral if low quality
→ morphological close (2×2) + CLAHE + brightness normalize
→ perspective correct (skip if no quad≥300×300 found or aspect change>30%)
→ crop_to_content() (Otsu → nonzero bbox + 5% margin, min 300×300)
```
Returns `(ndarray, metadata_dict)`. `detect_grid_structure()` → True if ≥6 h-lines AND ≥3 v-lines → routes to `extract_text_turbo()`.

---

## OCR Engines

**`engine.py` — OCREngine (EasyOCR)**
| Method | Canvas | MagRatio | Use |
|---|---|---|---|
| `extract_text()` | 1024 | 2.0 | Full quality / color phase |
| `extract_text_fast()` | 960 | 1.2 | Phase 1 fast pass |
| `extract_text_turbo()` | 640 | 1.0 | Structured/printed receipts |

JIT warmup on init (dummy 640×480 image → eliminates 5-8s first-scan cold start). All return `[{bbox, text, confidence, needs_review}]`.

**`azure_engine.py` — AzureOCREngine**
- `extract_receipt_structured(path)` → `{items, merchant, total, subtotal, tax, ocr_detections, ...}` (prebuilt-receipt)
- `extract_text_read(path)` → EasyOCR-compatible list (prebuilt-read)
- `extract_text_from_bytes(bytes, model)` → same as above but from in-memory bytes
- `_optimize_image_for_upload()` → JPEG 1500px 85q (4MB → ~300KB typical)
- Records usage even on failure. Polygon bboxes in clockwise TL,TR,BR,BL order.

---

## Usage Tracker (`usage_tracker.py`)

**`can_call_azure()` returns:**
```
allowed, reason, daily_used/remaining, monthly_used/remaining,
pace_status ("ok"/"fast"/"critical"), sustainable_daily_rate, days_left_in_month,
estimated_cost, is_within_free_tier
```
- **critical** = daily_used > sustainable × 1.5; **fast** = > 1.2× (warns but doesn't block)
- `record_call(model, pages, success)` — thread-safe, **atomic write** (tempfile→rename, prevents JSON corruption)
- `reset_daily()` — admin reset of today's counter

**Persistence** (`data/azure_usage.json`):
```json
{
  "days":   { "2026-03-03": { "calls": [...], "total_pages": 5 } },
  "months": { "2026-03": { "total_pages": 47, "read_pages": 45, "receipt_pages": 2, "estimated_cost": 0.067 } }
}
```
Daily entries auto-pruned after 7 days. Free tier = 500 pages/month = ~22 pages/day sustainable.

---

## Image Cache (`image_cache.py`)

- SHA-256 of file bytes → LRU OrderedDict (max 200 entries, 24h TTL)
- **Disk-persisted** to `data/image_cache.json` — survives server restarts
- `_make_json_safe()` handles numpy types. `_save_to_disk_unlocked()` runs inside threading.Lock.
- `get_stats()` returns `{size, hit_rate, azure_calls_saved, persisted: True/False}`
- `clear()` — wipes memory + disk JSON

---

## Receipt Parser (`parser.py` — 1402L)

**7 Regex patterns (priority order):**
1. `CODE - QTY` · 2. `CODE QTY` · 3. `QTY CODE` · 4. `CODE x QTY` · 5. `CODE: QTY` · 6. `CODE(QTY)` · 7. `^CODE$` (qty=1)

**4-tier code matching:** exact → OCR char subs (`|`→I, `0`→O, `6`→G …) → handwriting subs (`n`→H, `l`→I …) → fuzzy (difflib cutoff 0.5)

**QT-marker:** `Q1, QI, qt, &T, 4T` all recognized as quantity suffix.

**Pipeline:** group by Y (threshold = max(50, 5% height)) → skip patterns → clean → match → orphan qty association → aggregate duplicates (sum qty).

**Output:** `{receipt_id (REC-YYYYMMDD-HHMMSS-<uuid5>), items:[{code, product, quantity, unit, confidence, needs_review, match_type, raw_text}], total_items, avg_confidence, unparsed_lines}`

---

## REST API (`routes.py` — 576L)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | version, ocr_mode, azure_available, local_loaded |
| POST | `/api/receipts/scan` | 1MB chunk streaming, UUID suffix on filename, async via asyncio.to_thread |
| GET/DELETE | `/api/receipts/{id}` | 🔒 DELETE protected |
| GET | `/api/receipts` | `?limit=10` (1-100) |
| PUT | `/api/receipts/items/{id}` | ItemUpdate (code, name, qty validated) |
| POST | `/api/receipts/{id}/items` | Manual row add, 404 if receipt not found |
| GET | `/api/receipts/date/{date}` | YYYY-MM-DD |
| GET/POST/PUT/DELETE | `/api/products[/{code}]` | Full CRUD, soft-delete, pagination (limit/offset) |
| GET/POST | `/api/products/search` · `/api/products/export/csv` · `/api/products/import/csv` | CSV import validates: .csv ext, 1MB max, UTF-8 |
| POST/GET | `/api/export/excel` · `/api/export/daily` | |
| GET | `/api/export/download/{filename}` | Path traversal guard, .xlsx/.csv only |
| GET | `/uploads/{filename}` | Secure image serving (replaces raw static mount), image ext only |
| GET | `/api/dashboard` | Parallel DB queries via asyncio.gather (2-3× faster), includes `ocr_engine: get_engine_status()` |
| GET | `/api/ocr/status` | Full engine status |
| GET | `/api/ocr/usage` | usage + cache stats + **pacing** dict |
| POST | `/api/ocr/usage/reset-daily` | 🔒 API key protected |
| POST | `/api/ocr/cache/clear` | 🔒 API key protected |
| GET | `/` | → index.html |

**Pydantic validators:** `product_code` → strip, uppercase, `^[A-Z0-9_\-]{1,10}$`; `quantity` → 0 < qty ≤ 99999; names strip `<>{}\\`. **NumpyEncoder** handles np.integer/floating/bool_/ndarray in scan response.

---

## Security Middleware (`middleware.py` — 244L, ALL ACTIVE in `main.py`)

| Middleware | Details |
|---|---|
| `SecurityHeadersMiddleware` | X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy, Permissions-Policy |
| `RateLimitMiddleware` | Sliding 60s window per IP. Scan: 10 RPM, Others: 30 RPM. Returns 429 + Retry-After: 60. Thread-safe via `threading.Lock` |
| `APIKeyMiddleware` | Guards DELETE receipts/products + reset-daily + cache-clear. Skipped if `API_SECRET_KEY=""`. Header: `X-API-Key` |
| `DevTunnelCORSMiddleware` | Allows `*.devtunnels.ms` + `*.github.dev` dynamically. Uses `urlparse().hostname.endswith()` (not string contains — security fix). `Access-Control-Max-Age: 600` |

Order in main.py (outermost first): DevTunnel → SecurityHeaders → RateLimit → APIKey → CORSMiddleware → StaticCacheHeaders → RequestLogging

**Static cache headers middleware:** adds `Cache-Control: public, max-age=3600` for all `/static/` responses.

---

## Service Layer

**`receipt_service.py` (517L)** — 5-step pipeline:
1. `_save_uploaded_image()` → `uploads/receipt_YYYYMMDD_HHMMSS_<uuid6>.ext`
2. `preprocessor.preprocess()` → save processed image
3. `detect_grid_structure()` → `hybrid_engine.process_image(path, processed, is_structured)`
4. `azure_structured.items`? → `_parse_azure_structured()` else `parser.parse(ocr_detections)`. If Azure < 2 items → supplements with parser.
5. `db.create_receipt()` + `add_receipt_items()` + processing logs. DB failure sets `success: false`.

**`_parse_azure_structured()`** — 4-tier: azure-exact → azure-contains → azure-fuzzy (difflib 0.5) → azure-unmatched (first 6 chars, needs_review=True)

**`product_service.py` (197L)** — CRUD + CSV import/export, fuzzy search via difflib.  
**`excel_service.py` (386L)** — 2-sheet .xlsx: "Daily Sales Report" + "Summary" with OpenPyXL styles.

---

## Frontend (`app/static/`)

**`index.html` (459L)** — 3-tab SPA: Scan | Receipts | Catalog. Camera Scanner overlay with `<video>` viewfinder + canvas capture. Quick stats bar. Editable results table (code / name / qty / confidence / delete).

**`styles.css` (1833L)** — CSS custom properties: `--primary:#4F6BF6`, `--accent:#10B981`, 5 shadow levels, spring easing. Glassmorphic header, skeleton loading, toast notifications.

**`app.js` (1861L)** key behaviors:
- Client compress: resize >1800px → JPEG 0.88 before upload
- Camera: `getUserMedia()` → `canvas.toBlob()` → `processFile()`
- Clipboard paste: `document.addEventListener('paste')` (images only, scan tab)
- Keyboard: `1/2/3` tabs, `N` new scan, `C` camera, `Escape` close
- Auto-fill: typed product code → lookup `catalogCache` → fill name (green tint)
- Dashboard refresh every 30s; `perfState.processingTimes` keeps last 20 entries
- Batch mode toggle with beforeunload warning if unsaved results exist

---

## Startup Lifecycle (`main.py` — uses modern `@asynccontextmanager` lifespan)

**Startup:**
1. `setup_logging()` → log dirs/files
2. Mount static dir (but NOT /uploads or /exports — served via secure route endpoint)
3. Register all security middlewares + CORSMiddleware + static cache headers + request logging
4. Clean uploads AND exports older than 7 days
5. Pre-init `get_hybrid_engine()` (creates usage_tracker + image_cache)
6. If mode ≠ `azure` → pre-load `get_ocr_engine()` (pays EasyOCR cold start at launch)
7. Log: API URL, docs URL, log file path

**Shutdown:**
- `db.shutdown()` → closes all pooled connections

---

## OCR Deep Audit & Optimization (Sessions 44-45)

### Accuracy Benchmarks (5 test receipt images)

| Metric | Pre-Optimization | Post-Optimization | Change |
|--------|------------------|--------------------|--------|
| **Code Detection** | 56-68% | **96%** (25/26) | **+40 pts** |
| **Qty Accuracy** | 32-46% | **69%** (18/26) | **+33 pts** |

**Per-image results:**
| Image | Code Det. | Qty Acc. | Notes |
|-------|-----------|----------|-------|
| receipt_neat | 80% | 40% | PEPW1→PEPW1O OCR ambiguity (unresolvable) |
| receipt_messy | **100%** | **100%** | 🎉 Perfect (was 25%) |
| receipt_faded | **100%** | **100%** | ✅ Perfect (maintained) |
| receipt_dense | **100%** | 50% | TEW10/TEW20 swap + missing qty digits |
| receipt_dark_ink | **100%** | 80% | PEPW10 qty=2 undetected by OCR |

### Optimization Changes Applied

**P0 — Config Tuning:**
- `IMAGE_MAX_DIMENSION` 1280→1800, `OCR_CANVAS_SIZE` 1024→1536, `OCR_MIN_SIZE` 20→10
- `adjust_contrast` 0.7→0.9 (preserves faded ink), `width_ths` 0.6→0.8 (keeps alphanum codes)

**P1 — Preprocessor & Parser:**
- Morphological closing now conditional (only for blurry images)
- Shadow normalization with bg_std > 15 guard (skips uniform illumination)
- Contrast stretch for low-contrast images (percentile-based)
- Code reassembly in `_clean_ocr_text`: PEPW + 20 → PEPW20 via catalog lookup
- Pipe splitting: `|` treated as line separator
- `_quick_item_count` uses `isalnum()` for alphanumeric code matching

**P2 — Parser Intelligence:**
- **Rotation-resistant line grouping**: right-column digits use 1.25× Y-threshold, computed against left-column code positions only (prevents rotation-induced quantity shift)
- Position-aware overlap scoring with threshold 0.7
- Quality gate brightness threshold raised to 252
- `_merge_local_passes` Y-bucket widened to 60px

**P3 — Edge Cases:**
- `_CODE` regex: min 3 chars for pure alpha (prevents "TH", "IN" matches)
- Trailing O/I ambiguity resolution in `_map_product_code`
- Duplicate code ambiguity resolver (PEPW1O→PEPW10 when PEPW1 also exists)
- CODE+QTY reassembly check in `_parse_line` (PEPW + 20 → PEPW20)
- Single-char handwriting digit mapping only after code token found
- Orphan qty Y-proximity guard (max_y × 0.04)

### Remaining Limitations (EasyOCR on CPU)
- OCR O/1 confusion: "PEPW1" reads as "PEPW1O" (indistinguishable from "PEPW10")
- Some quantity digits simply not detected on dense/dark images
- Hybrid engine multi-pass merge can swap Y positions across rows
- Azure Document Intelligence would significantly improve all these cases

---

## Known Behaviors / Edge Cases

- First run: ~500MB EasyOCR model download. Subsequent runs use `models/` cache.
- `AZURE_DOC_INTEL_AVAILABLE` set at import time — credential changes need server restart.
- `receipt-then-read` strategy can consume **2 Azure pages** per scan — avoid as default.
- Budget pacing warns but does **not** block — use daily/monthly limits for hard stops.
- `data/` dir is lazy-created; `data/image_cache.json` starts fresh if corrupted (silent).
- Receipt number `REC-YYYYMMDD-HHMMSS-<uuid5>` — UUID suffix prevents same-second collision.
- `HYBRID_CROSS_VERIFY=True` doubles Azure cost — only enable for accuracy benchmarking.
- `AUTO_SAVE_INTERVAL_SECONDS` (30s) and `MAX_RECEIPTS_PER_BATCH` (50) are defined but **not implemented**.
- Images saved 3× per scan: raw upload + copy + processed. Both upload + export dirs cleaned at startup (>7 days).
- `/uploads/` and `/exports/` are NOT mounted as static directories — served via route-level endpoints with filename validation and extension allowlisting (prevents directory browsing and arbitrary file access).
- SQLite daily backup runs automatically before first write of each day (backups pruned after 7 days).
- Schema migrations are tracked in `schema_migrations` table — never run twice. New migration = add function + tuple to MIGRATIONS list.
- PostgreSQL backend is a drop-in swap: `DB_BACKEND=postgresql` — same data shapes, zero service/route changes.
- `db.shutdown()` must be called on app exit to close pooled connections (handled by lifespan handler).
- Dashboard endpoint runs 3 DB queries in parallel via `asyncio.gather()` for 2-3× speedup.

---

## Quick Reference

```
run.py → python run.py            (server on :8000)
tests  → python -m pytest tests/  
e2e    → python test_all_flows.py
ocr    → python test_ocr_accuracy.py  (5-image accuracy benchmark)
bench  → python benchmark_pipeline.py
debug  → python debug_qty.py / python dump_ocr.py / python debug_ocr.py
gen    → python generate_test_receipts.py  (regenerate test_images/)
```

**Singletons:** `db` (via `get_database()`), `product_service`, `receipt_service`, `excel_service` = eager at import.  
**Lazy singletons:** `get_ocr_engine()`, `get_azure_engine()`, `get_hybrid_engine()`, `get_usage_tracker()`, `get_image_cache()` = created on first call.

**Free tier math:** 500 pages/month ÷ 22 work days ≈ 22 pages/day. With local-first skip (conf≥0.72 AND detects≥4) ~40-60% of scans skip Azure → effective ~35-50 scans/day.
