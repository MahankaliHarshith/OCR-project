# Project Context — Handwritten Receipt Scanner

**Stack:** Python 3.12, FastAPI, EasyOCR, Azure Document Intelligence, OpenCV, SQLite (WAL) / PostgreSQL, Vanilla JS SPA  
**Purpose:** Local web app for small shops to scan handwritten receipts → extract product codes + quantities → save to DB → export Excel.  
**Key constraint:** Operates within Azure free tier (500 pages/month) via 6-layer cost defense. Works fully offline with local EasyOCR alone.  
**Latest audit:** 🏆 **91/100 (Grade A)** — 100% code detection, 100% qty accuracy on synthetic images, 0 critical failures.  
**Smart OCR:** Phase 2 complete — duplicate detection, quality scoring (0–100 + letter grade), validation rules, OCR correction feedback loop, date/store extraction. 7 bugs found & fixed via deep edge-case testing.  
**Tests:** **314 tests passing** (283 unit + 31 integration) across 15+ test files.

---

## Directory Layout

```
OCR project/
├── run.py / start_server.py / start_public.py   # Server launchers (direct / subprocess / ngrok)
├── requirements.txt / .env.example / README.md
├── pyproject.toml / Dockerfile / docker-compose.yml
├── docs/
│   ├── CONTEXT.md                                # This file — project context & architecture
│   ├── DEEP_AUDIT_REPORT.md                      # Audit results (91/100 A) + training guide
│   ├── HYBRID_OCR_ARCHITECTURE.md
│   ├── AI_Receipt_Generation_Prompts.md
│   ├── Receipt_Design_and_Scanning_Guide.md
│   └── PRD.txt
├── app/
│   ├── config.py (240L)         main.py (254L)   middleware.py (232L)
│   ├── database.py (1328L)      db_postgres.py (449L)
│   ├── logging_config.py (125L) json_logging.py (136L)
│   ├── observability.py (327L)  tracing.py (187L)   metrics.py (122L)
│   ├── error_tracking.py (175L) websocket.py (92L)
│   ├── api/routes.py (974L)     ← +54L for Smart OCR endpoints
│   ├── ocr/
│   │   ├── preprocessor.py (1002L)  engine.py (374L)     parser.py (2468L)
│   │   ├── azure_engine.py (570L)   hybrid_engine.py (1192L)
│   │   ├── total_verifier.py (714L) quality_scorer.py (175L) validators.py (186L)
│   │   ├── usage_tracker.py (364L)  image_cache.py (261L)
│   ├── services/
│   │   ├── receipt_service.py (980L)  product_service.py (169L)  excel_service.py (327L)
│   │   ├── batch_service.py (441L)    dedup_service.py (131L)    correction_service.py (111L)
│   ├── static/
│   │   ├── index.html (954L)  styles.css (2991L)  app.js (3985L)  lucide.min.js
│   └── training/
│       ├── routes.py (352L)  benchmark.py (358L)  optimizer.py (290L)
│       ├── data_manager.py (284L)  template_learner.py (303L)
│       └── real_world_trainer.py (909L)  # Adaptive trainer with error mining + learned rules
├── scripts/
│   ├── start_server.py  start_public.py  start_devtunnel.py  train.py  trainer.py (530L)
│   ├── dev/ benchmark_azure_vs_local.py  benchmark_pipeline.py  diag_edge.py  dump_ocr.py
│   └── generators/ create_test_receipt.py  generate_edge_case_receipts.py  generate_test_receipts.py
├── tests/
│   ├── test_smart_ocr.py (702L)  test_smart_ocr_edge_cases.py (991L)
│   ├── test_accuracy.py (246L)   test_api.py (157L)   test_app.py (201L)
│   ├── test_services.py (297L)    test_infrastructure.py (432L)
│   ├── test_middleware_and_db.py (450L)   test_parser_internals.py (365L)
│   ├── test_azure_integration.py (348L)  test_db_production.py (488L)
│   ├── test_observability.py (307L)  test_preprocessing.py (215L)  test_training.py (478L)
│   ├── test_trainer.py (482L)
│   ├── e2e/  test_all_flows.py  test_realworld_audit.py (497L)  run_deep_test.py
│   │         test_edge_cases.py  test_ocr_accuracy.py  test_new_samples.py  ...
│   ├── integration/ test_comprehensive.py  test_boxed_template.py  test_qty.py ...
│   └── fixtures/
├── monitoring/  prometheus.yml  alertmanager.yml  alert_rules.yml  loki.yml  promtail.yml  grafana/
├── training_data/  images/  labels/  profiles/  results/  augmented/  labels_template.json
├── uploads/ exports/ models/ logs/ data/ backups/
└── receipt_scanner.db
```

---

## Environment Variables (all tunable via `.env`)

| Variable | Default | Notes |
|---|---|---|
| `OCR_ENGINE_MODE` | `auto` | `auto` / `azure` / `local` |
| `AZURE_MODEL_STRATEGY` | `receipt-only` | `receipt-only`($0.01/pg) · `read-only`($0.0015/pg) · `receipt-then-read`(may use 2 pages!) |
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | `""` | Activates Azure when set |
| `AZURE_DOCUMENT_INTELLIGENCE_KEY` | `""` | |
| `AZURE_DAILY_PAGE_LIMIT` | `50` | Hard stop; falls back to local |
| `AZURE_MONTHLY_PAGE_LIMIT` | `500` | = free tier |
| `LOCAL_CONFIDENCE_SKIP_THRESHOLD` | `0.85` | Skip Azure if local conf ≥ this |
| `LOCAL_MIN_DETECTIONS_SKIP` | `4` | Both conf AND detections must pass to skip Azure |
| `LOCAL_CATALOG_MATCH_SKIP_THRESHOLD` | `0.3` | Min catalog match rate to trust local results |
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
| `SENTRY_DSN` | `""` | Sentry error tracking (optional) |
| `SENTRY_ENVIRONMENT` | `development` | Sentry environment tag |
| `OTEL_TRACING_ENABLED` | `false` | Enable OpenTelemetry distributed tracing |
| `OTEL_EXPORTER_ENDPOINT` | `localhost:4317` | OTLP endpoint for trace export |

**dotenv:** `config.py` calls `load_dotenv(BASE_DIR / ".env")` at import (silent if python-dotenv missing).

---

## Key Config Constants (`app/config.py` — 227L)

```
Paths:     BASE_DIR / uploads / exports / models / logs / data / backups (all auto-created)
           DATABASE_PATH auto-redirects to %LOCALAPPDATA%/ReceiptScanner if in OneDrive folder

EasyOCR:   CANVAS_SIZE=1280 (optimized from 1536 for same-receipt-type speed)
           MAG_RATIO=1.8 (optimized from 2.0 — ~15% speed gain)
           MIN_SIZE=10, CONFIDENCE_THRESHOLD=0.40, USE_GPU=False
           SMART_PASS_THRESHOLD=3 (skip 2nd OCR pass once 3+ items found)
           PARALLEL_DUAL_PASS=True (ThreadPoolExecutor dual-pass)

Azure:     AZURE_API_TIMEOUT=30, AZURE_IMAGE_MAX_DIMENSION=1500, AZURE_IMAGE_QUALITY=85
           AZURE_RECEIPT_CONFIDENCE_THRESHOLD=0.6, AZURE_RECEIPT_MIN_ITEMS=1
           HYBRID_CROSS_VERIFY=False  (True = always run local after Azure → doubles cost)

Preprocess: IMAGE_MAX_DIMENSION=1800, CLAHE_CLIP_LIMIT=2.0, CLAHE_TILE_GRID=(8,8)

Excel:     EXCEL_HEADER_COLOR="4472C4", ALT_ROW_COLOR="F2F2F2", LOW_CONF_COLOR="FFD966"

Fuzzy:     FUZZY_MATCH_CUTOFF=0.72, FUZZY_MAX_RESULTS=5
           Adaptive cutoff: ≤3 chars=0.88, ≤4=0.82, ≤6=0.72, >6=0.65

App:       MAX_FILE_SIZE_MB=20, ALLOWED_EXTENSIONS={jpg,jpeg,png,bmp,tiff,webp}
           API_DOCS_ENABLED=True (controls /docs and /redoc endpoints)
```

---

## Database Architecture (`database.py` — 1328L)

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
products:          id, product_code(UNIQUE), product_name, category, unit, unit_price, is_active, created_at, updated_at
receipts:          id, receipt_number(UNIQUE), scan_date, scan_time, image_path, processed_image_path,
                   processing_status, total_items, ocr_confidence_avg, created_at,
                   image_hash, content_fingerprint, receipt_date, store_name,     ← v4 Smart OCR
                   quality_score, quality_grade                                    ← v4 Smart OCR
receipt_items:     id, receipt_id(FK CASCADE), product_code, product_name, quantity, unit,
                   unit_price, line_total,                                         ← v3 price columns
                   ocr_confidence, manually_edited
ocr_corrections:   id, receipt_id(FK SET NULL), item_id, original_code, corrected_code,  ← v4 Smart OCR
                   original_qty, corrected_qty, raw_ocr_text, created_at
processing_logs:   id, receipt_id(FK no-cascade), stage, status, duration_ms, error_message, timestamp
schema_migrations: version(PK), name, applied_at   ← migration tracking (v1–v4)
```

### Schema Migrations
| Version | Name | Purpose |
|---|---|---|
| v1 | `baseline_schema` | Original tables (products, receipts, receipt_items, processing_logs) |
| v2 | `composite_item_index` | Composite index on receipt_items(receipt_id, product_code) |
| v3 | `add_price_columns` | Add unit_price + line_total to receipt_items |
| v4 | `smart_ocr_metadata` | Add image_hash, content_fingerprint, receipt_date, store_name, quality_score, quality_grade to receipts + create ocr_corrections table |

### Seed Data (18 products)
**Alpha codes:** ABC(1L Exterior Paint), XYZ(1L Interior Paint), PQR(5L Primer), MNO(Paint Brush), DEF(1L Wood Varnish), GHI(Sandpaper), JKL(Putty Knife), STU(Wall Filler), VWX(Masking Tape), RST(Thinner 500ml)  
**TEW series:** TEW1(₹250), TEW4(₹850), TEW10(₹1800), TEW20(₹3200) — Thinnable Exterior Wash  
**PEPW series:** PEPW1(₹350), PEPW4(₹1200), PEPW10(₹2600), PEPW20(₹4800) — Premium Exterior Premium Wash

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

---

## PostgreSQL Backend (`db_postgres.py` — 450L)

- Drop-in replacement: set `DB_BACKEND=postgresql` + `POSTGRES_*` env vars
- Uses `psycopg2.pool.ThreadedConnectionPool` (min 2, max 10 connections)
- `RealDictCursor` for dict-like row access
- All methods return identical data shapes to SQLite — zero service/route changes needed
- Auto-creates tables on init (same schema as SQLite, adapted for PG syntax)
- Requires: `pip install psycopg2-binary`

---

## Hybrid OCR Pipeline (AUTO mode — `hybrid_engine.py` — 1170L)

```
[0] image_cache.get(SHA-256)          → HIT: return free          (strategy: auto-cached)
[1] _check_image_quality()            → sharpness<30 OR dark<40:
                                         return local only         (auto-quality-gate)
[2] _run_local_pipeline() (free)
    crop → turbo/fast → count items
    → optional color EXIF-corrected pass
    conf≥0.85 AND detections≥4
    AND catalog_match≥30%?            → return local              (auto-local-skip)
[3] usage_tracker.can_call_azure()    → blocked?  return local     (auto-usage-limited)
[4] Azure — single model per AZURE_MODEL_STRATEGY
    "receipt-only"     → prebuilt-receipt  $0.01     ← DEFAULT
    "read-only"        → prebuilt-read     $0.0015
    "receipt-then-read"→ receipt then read (up to 2 pages!)
    → image_cache.put() → return                                   (auto-azure-read/receipt)
[5] Azure failed → return local                                    (auto-fallback-local)
[Optional] HYBRID_CROSS_VERIFY=True → run local again to cross-check (1.15× boost on overlap)
```

### Smart-Skip Dual-Pass Logic (v2.0.0)
- **Serial dual-pass**: When parallel is disabled or no color image available
  - Run gray fast-pass → parse items → if items ≥ `OCR_SMART_PASS_THRESHOLD` (3) **AND** confidence ≥ 0.55 → skip 2nd pass
  - Logs skip decision with item count and confidence value
- **Parallel dual-pass**: ThreadPoolExecutor runs gray + color simultaneously, merges results
- **Y-distance dedup**: 1-2 digit numbers use 35px raw Y-distance (prevents cascading collapse of repeated qty digits)
- **Position-based echo dedup**: Same physical (x,y) within 30×15px from different passes → keep best confidence

---

## Preprocessing Pipeline (`preprocessor.py` — 1014L)

```
Raw image → _load_with_exif_correction() → resize (max 1800px)
→ grayscale → deskew (HoughLinesP, ±15°, skip if angle < 1.5° for speed)
→ quality check (Laplacian + brightness)
→ enhance: Gaussian blur(3,3) + unsharp if blurry + bilateral if low quality
→ morphological close (conditional, only for blurry images) + CLAHE + brightness normalize
→ shadow normalization (bg_std > 15 guard — skips uniform illumination)
→ contrast stretch for low-contrast images (percentile-based)
→ perspective correct (skip if no quad≥300×300 found or aspect change>30%)
→ crop_to_content() (Otsu → nonzero bbox + 5% margin, min 300×300)
```

Returns `(ndarray, metadata_dict)`. `detect_grid_structure()` → True if ≥6 h-lines AND ≥3 v-lines → routes to `extract_text_turbo()`.

**Deskew optimization (v2.0.0):** Threshold raised from 0.5° to 1.5° — skips minor rotations on well-aligned same-type receipt photos for speed. Angles 0.5°–1.5° logged but not corrected.

---

## OCR Engines

### `engine.py` — OCREngine (EasyOCR) — 373L

| Method | Canvas | MagRatio | width_ths | Notes |
|---|---|---|---|---|
| `extract_text()` | 1280 | 1.8 | 0.7 | Full quality / color phase, dynamic per-image tuning |
| `extract_text_fast()` | 1024 | 1.5 | 0.7 | Phase 1 fast pass — strong enough to skip 2nd pass |
| `extract_text_turbo()` | 640 | 1.0 | 0.7 | Structured/printed receipts |

**v2.0.0 parameter changes:**
- `extract_text()`: canvas 1536→1280, mag_ratio 2.0→1.8, width_ths 0.8→0.7 (keeps alphanumeric codes TEW1/PEPW10 together)
- `extract_text_fast()`: canvas 960→1024, mag_ratio 1.2→1.5 (captures more detail on first pass), adjust_contrast 0.7→0.8, add_margin 0.1→0.12
- Dynamic quality-based tuning: blurry → lower thresholds + higher mag; dark → increased contrast; low contrast → lower contrast_ths

JIT warmup on init (dummy 1024×768 image → eliminates 5-8s first-scan cold start). All return `[{bbox, text, confidence, needs_review}]`.

### `azure_engine.py` — AzureOCREngine — 579L
- `extract_receipt_structured(path)` → `{items, merchant, total, subtotal, tax, ocr_detections, ...}` (prebuilt-receipt)
- `extract_text_read(path)` → EasyOCR-compatible list (prebuilt-read)
- `extract_text_from_bytes(bytes, model)` → same as above but from in-memory bytes
- `_optimize_image_for_upload()` → JPEG 1500px 85q (4MB → ~300KB typical)
- Records usage even on failure. Polygon bboxes in clockwise TL,TR,BR,BL order.

---

## Bill Total Verification (`total_verifier.py` — 797L)

4-layer architecture for receipt total verification:

| Layer | Purpose |
|---|---|
| **1. Total Line Extraction** | Parse OCR detections for "Total Qty: N" / "Grand Total: N" using spatial analysis (bottom-of-receipt heuristic) + keyword matching |
| **2. Multi-Pass Digit Re-Reading** | Multiple OCR passes with different preprocessing (original, contrast-enhanced, binarized) + majority vote |
| **3. Arithmetic Reconciliation** | Compare OCR-read total vs computed sum of item quantities, flag mismatches with confidence-weighted severity |
| **4. Dispute Resolution** | When OCR total ≠ computed total, determine which is more trustworthy using item-level confidence scores + total-line confidence |

**OCR-garbled total variants recognized:** qtyt, qiy, qtt, grramd, gramd, grrand, totol, totai, etc.

---

## Quality Scorer (`quality_scorer.py` — 175L)

Computes a 0–100 quality score and letter grade (A/B/C/D) per receipt:

| Factor | Points | Description |
|---|---|---|
| OCR Confidence | 30 | avg confidence 0.5→0, 1.0→30 |
| Items Found | 20 | 3+ items → 20 |
| Total Verification | 15 | qty total matches → 15 |
| Math Verification | 15 | all line math OK → 15 |
| Image Quality | 10 | sharpness + brightness |
| Catalog Match Rate | 10 | % items matched to catalog |

**Bug fixes applied (v2.1):**
- Items without `match_type` key no longer inflate catalog match score (added `None` to exclusion list)
- `sharpness=0` / `brightness=0` now display as `0.0` instead of `None` (was using falsy check instead of `is not None`)
- `None` confidence values no longer crash `> 0` comparison (uses `or 0` fallback)

---

## Receipt Validator (`validators.py` — 186L)

Post-parse validation rules engine:
1. **Impossible quantity detection** — zero, negative, absurdly high (MAX_REASONABLE_QTY=100, MAX_ABSOLUTE_QTY=999)
2. **Price sanity checks** — missing prices, extreme deviations (>5× catalog), math errors
3. **Duplicate item flagging** — same code appears multiple times
4. **Cross-receipt anomaly detection** — qty far exceeds historical patterns (no longer gated on catalog — **bug fix v2.1**)

---

## Usage Tracker (`usage_tracker.py` — 366L)

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

## Image Cache (`image_cache.py` — 262L)

- SHA-256 of file bytes → LRU OrderedDict (max 200 entries, 24h TTL)
- **Disk-persisted** to `data/image_cache.json` — survives server restarts
- `_make_json_safe()` handles numpy types. `_save_to_disk_unlocked()` runs inside threading.Lock.
- `get_stats()` returns `{size, hit_rate, azure_calls_saved, persisted: True/False}`
- `clear()` — wipes memory + disk JSON

---

## Receipt Parser (`parser.py` — 2407L)

**7 Regex patterns (priority order):**
1. `CODE - QTY` · 2. `CODE QTY` · 3. `QTY CODE` · 4. `CODE x QTY` · 5. `CODE: QTY` · 6. `CODE(QTY)` · 7. `^CODE$` (qty=1)

**4-tier code matching:** exact → OCR char subs (`|`→I, `0`→O, `6`→G …) → handwriting subs (`n`→H, `l`→I …) → fuzzy (difflib, adaptive cutoff by code length)

**QT-marker:** `Q1, QI, qt, &T, 4T` all recognized as quantity suffix.

**Pipeline:** group by Y (adaptive threshold, dense receipt detection at ≥6 items) → skip patterns → clean → match → orphan qty association → aggregate duplicates (sum qty).

**Qty Sanity (v2.0.0):**
- qty == 0 → reset to 1, flag needs_review
- qty > 100 (no price data) → reset to 1 (catches OCR artefacts like VWX qty=240)
- qty > 999 (with price data, 4-col) → reset to 1
- qty 100–999 (with price data) → trust but flag for review
- 2-col qty clamp at 50 (was 99)

**Dense receipt detection (v2.0.0):**
- Activated at ≥6 detections (was 8)
- Gap factor 0.70 (was 0.80) — tighter to prevent PEPW1↔PEPW10 swap on dense 8-item receipts
- Min gap 3px, min threshold 12px (was 5px / 15px)

**Output:** `{receipt_id (REC-YYYYMMDD-HHMMSS-<uuid5>), items:[{code, product, quantity, unit, confidence, needs_review, match_type, raw_text}], total_items, avg_confidence, unparsed_lines}`

---

## REST API (`routes.py` — 974L)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | version, ocr_mode, azure_available, local_loaded |
| POST | `/api/receipts/scan` | 1MB chunk streaming, UUID suffix on filename, async via asyncio.to_thread |
| POST | `/api/receipts/batch-async` | Async batch processing, returns batch_id immediately |
| GET | `/api/batch/{id}` | Poll batch job status |
| GET/DELETE | `/api/receipts/{id}` | 🔒 DELETE protected |
| GET | `/api/receipts` | `?limit=10` (1-100) |
| PUT | `/api/receipts/items/{id}` | ItemUpdate (code, name, qty validated), records OCR correction feedback |
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
| GET | `/api/corrections` | OCR correction feedback statistics (Smart OCR) |
| GET | `/api/item-stats` | Per-product historical quantity statistics (Smart OCR) |
| WS | `/ws/batch/{batch_id}` | Real-time batch processing updates |
| GET | `/` | → index.html |

**Pydantic validators:** `product_code` → strip, uppercase, `^[A-Z0-9_\-]{1,10}$`; `quantity` → 0 < qty ≤ 99999; names strip `<>{}\\`. **NumpyEncoder** handles np.integer/floating/bool_/ndarray in scan response.

---

## Batch Processing (`batch_service.py` — 514L)

Async background job processing for scanning multiple receipts:
- `POST /api/receipts/batch-async` → validates files, saves to disk, returns `batch_id` immediately
- Background worker uses ThreadPoolExecutor for parallel OCR
- Poll status via `GET /api/batch/{batch_id}` or subscribe via WebSocket `/ws/batch/{batch_id}`
- Tracks per-file status: pending → processing → completed/failed

---

## WebSocket Manager (`websocket.py` — 121L)

Real-time batch processing updates via `ws://host:port/ws/batch/{batch_id}`:
- Message types: `batch_started`, `file_completed`, `file_failed`, `batch_completed`, `error`
- Thread-safe with `asyncio.Lock` on subscriber dict
- Eliminates polling for batch status

---

## Service Layer

**`receipt_service.py` (980L)** — 6-step pipeline:
1. `_save_uploaded_image()` → `uploads/receipt_YYYYMMDD_HHMMSS_<uuid6>.ext`
2. `preprocessor.preprocess()` → save processed image
3. `detect_grid_structure()` → `hybrid_engine.process_image(path, processed, is_structured)`
4. `azure_structured.items`? → `_parse_azure_structured()` else `parser.parse(ocr_detections)`. If Azure < 2 items → supplements with parser.
5. `db.create_receipt()` + `add_receipt_items()` + processing logs. DB failure sets `success: false`.
6. **Post-parse Smart OCR pipeline** (Steps 4b–4f):
   - 4b: Bill total verification (multi-pass digit re-reading)
   - 4c: Math/price verification (catalog price injection)
   - 4d: Smart validation rules (qty anomaly, price deviation, duplicates, historical anomalies)
   - 4e: Quality scoring (0–100 + letter grade A/B/C/D)
   - 4f: Dedup hash computation + duplicate check against recent receipts
   - Save smart OCR metadata (image_hash, content_fingerprint, receipt_date, store_name, quality_score, quality_grade)

**`_parse_azure_structured()`** — 4-tier: azure-exact → azure-contains → azure-fuzzy (difflib 0.5) → azure-unmatched (first 6 chars, needs_review=True)

**`dedup_service.py` (131L)** — 3-layer duplicate detection: perceptual image hash (8×8 grayscale average hash, hamming distance ≤5) + content fingerprint (SHA-256 of sorted code:qty pairs) + user confirmation prompt. 24h dedup window. Bug fixes: handles SQL NULL hashes, None codes, empty fingerprints.  
**`correction_service.py` (111L)** — OCR correction feedback loop: records user corrections (code/qty changes), builds lookup map from corrections with ≥2 occurrences (filters noise), thread-safe cache with invalidation. Parser checks this map before fuzzy matching.  
**`product_service.py` (169L)** — CRUD + CSV import/export, fuzzy search via difflib.  
**`excel_service.py` (327L)** — 2-sheet .xlsx: "Daily Sales Report" + "Summary" with OpenPyXL styles.

---

## Security Middleware (`middleware.py` — 238L, ALL ACTIVE in `main.py`)

| Middleware | Details |
|---|---|
| `SecurityHeadersMiddleware` | X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy, Permissions-Policy |
| `RateLimitMiddleware` | Sliding 60s window per IP. Scan: 10 RPM, Others: 30 RPM. Returns 429 + Retry-After: 60. Thread-safe via `threading.Lock` |
| `APIKeyMiddleware` | Guards DELETE receipts/products + reset-daily + cache-clear. Skipped if `API_SECRET_KEY=""`. Header: `X-API-Key` |
| `DevTunnelCORSMiddleware` | Allows `*.devtunnels.ms` + `*.github.dev` dynamically. Uses `urlparse().hostname.endswith()` (not string contains — security fix). `Access-Control-Max-Age: 600` |

Order in main.py (outermost first): DevTunnel → SecurityHeaders → RateLimit → APIKey → CORSMiddleware → StaticCacheHeaders → RequestLogging

**Static cache headers middleware:** adds `Cache-Control: public, max-age=3600` for all `/static/` responses.

---

## Observability Stack

### Dynamic Observability Manager (`observability.py` — 393L)
Monitors app health (error rate, latency, throughput) and auto-adjusts settings:
- Low traffic / no errors → minimal logging, low trace sampling
- Errors spiking → auto-enable DEBUG logs, increase sampling
- Latency degrading → flag slow operations
- Uses simple ring buffer, no threads, no I/O — zero overhead
- CAN auto-adjust: log verbosity, trace sampling, internal alerts
- CANNOT auto-start: Sentry (needs DSN), Prometheus (needs server), OTel (needs endpoint)

### OpenTelemetry Tracing (`tracing.py` — 237L)
Request-level distributed tracing across the full scan pipeline:
- HTTP request → preprocessing → OCR engine → parsing → verification → DB save
- Environment-driven: `OTEL_TRACING_ENABLED=true` to enable
- Compatible: Jaeger, Grafana Tempo, Azure Monitor, Zipkin (via OTLP)
- `optional_span()` context manager — no-op when tracing disabled

### Prometheus Metrics (`metrics.py` — 158L)
Exposes at `/metrics` for Prometheus scraping:
- **Auto HTTP**: request count, duration histogram, in-progress gauge
- **Business**: `receipt_scans_total`, `receipt_scan_duration_seconds`, `ocr_items_detected`, `ocr_confidence_score`, `azure_api_calls_total`, `azure_pages_used_daily/monthly`, `cache_hits/misses_total`, `db_connections_active`, `rate_limit_rejections_total`

### Error Tracking (`error_tracking.py` — 212L)
Sentry integration for production:
- Catches unhandled exceptions, OCR pipeline failures, slow operations
- `SENTRY_DSN` env var to enable — all functions are no-ops when disabled
- Auto-instruments FastAPI, SQLite, HTTP calls

### JSON Structured Logging (`json_logging.py` — 139L)
Machine-parseable JSON log output for production log aggregation (ELK, Loki, etc.)

### Monitoring Config (`monitoring/`)
Pre-configured: `prometheus.yml`, `alertmanager.yml`, `alert_rules.yml`, `loki.yml`, `promtail.yml`, `grafana/` dashboards + provisioning

---

## Training System (`app/training/` — 1521L total)

### Architecture
```
Training Data Pipeline:
  upload labeled images → data_manager stores → benchmark evaluates
  → optimizer tunes OCR params → template_learner builds receipt profiles
```

### Modules

| Module | Lines | Purpose |
|---|---|---|
| `routes.py` | 430 | 23 API endpoints under `/api/training/` (16 original + 7 trainer) |
| `benchmark.py` | 360 | Accuracy benchmarking against labeled ground truth |
| `optimizer.py` | 292 | Auto-tune OCR parameters for specific receipt types |
| `data_manager.py` | 286 | Manage training images + labels + profiles |
| `template_learner.py` | 306 | Build receipt templates from repeated scans |
| `real_world_trainer.py` | 650 | Adaptive real-world training engine with error mining + learned rules |

### Key Endpoints
- `POST /api/training/upload` — Upload labeled training image
- `POST /api/training/benchmark` — Run accuracy benchmark
- `POST /api/training/optimize` — Auto-tune OCR parameters
- `POST /api/training/learn` — Learn receipt template from samples
- `GET /api/training/params` — View current OCR parameters
- `GET /api/training/profiles` — List learned receipt profiles

### Real-World Trainer Endpoints
- `POST /api/training/trainer/scan` — Scan an image and return corrections interface
- `POST /api/training/trainer/save` — Save corrected receipt as training sample
- `POST /api/training/trainer/analyze` — Mine error patterns from collected samples
- `POST /api/training/trainer/learn` — Generate learned character/code substitution rules
- `GET /api/training/trainer/confusion` — Get confusion matrix (OCR misread statistics)
- `POST /api/training/trainer/auto-improve` — Run full improvement cycle (analyze → learn → export)
- `GET /api/training/trainer/report` — Generate training progress report

### Real-World Trainer (`real_world_trainer.py` — 650L)

Adaptive training engine that learns from real-world scanning corrections to continuously improve OCR accuracy.

**Core workflow:** Scan receipt → human corrects errors → system mines error patterns → builds confusion matrix → generates learned rules → rules auto-loaded by parser on next scan.

**Capabilities:**
1. **Scan & Correct** — `scan_receipt()` + `save_corrected_sample()`: OCR a real receipt, let user correct errors, save as labeled training data
2. **Error Pattern Mining** — `mine_error_patterns()`: Needleman-Wunsch string alignment to find systematic OCR misreads (e.g., "O"→"0", "l"→"1")
3. **Confusion Matrix** — `build_confusion_matrix()`: Statistical character-level confusion analysis across all training samples
4. **Learned Rules Generation** — `generate_learned_rules()`: Auto-generates character substitution, reverse substitution, and product code correction rules
5. **Image Augmentation** — `augment_images()`: Rotation, noise, blur, brightness, contrast, perspective transforms for training data expansion
6. **Auto-Improve Cycle** — `run_improvement_cycle()`: Full pipeline (mine → confusion → learn → export) in one call
7. **Progress Reports** — `generate_report()`: Training statistics, accuracy trends, top error patterns
8. **Batch Scanning** — `batch_scan()`: Process multiple images for bulk training data collection

**Algorithm highlights:**
- Needleman-Wunsch alignment (`_align_strings()`) for character-level diff
- Levenshtein distance for string similarity scoring
- Confidence-weighted confusion pairing
- Minimum frequency thresholds to avoid noise in learned rules

**Persisted files:**
- `training_data/learned_rules.json` — Auto-loaded by `parser.py` on startup
- `training_data/results/confusion_matrix.json` — Character confusion statistics
- `training_data/results/error_patterns.json` — Systematic OCR misread patterns
- `training_data/training_sessions.json` — Session metadata history
- `training_data/correction_log.json` — All human corrections log

**Parser integration:** `parser.py.__init__()` calls `_load_learned_rules()` which reads `training_data/learned_rules.json` and applies learned character substitutions, reverse substitutions, and code corrections in `_generate_ocr_variants()`.

### Interactive CLI (`scripts/trainer.py` — 530L)

9-command interactive CLI for real-world training workflows:

```
Commands:
  scan           Scan a receipt image, review & correct OCR results
  batch-scan     Batch scan multiple images from a directory
  analyze        Mine error patterns from collected training data
  learn          Generate learned substitution rules from error patterns
  confusion      Display character confusion matrix
  auto-improve   Run full improvement cycle (analyze → learn → export)
  report         Generate training progress report
  augment        Augment training images with transforms
  status         Show training data statistics
```

Run: `python scripts/trainer.py [command]` or `python scripts/trainer.py` for interactive menu.

### Training Data Structure
```
training_data/
├── images/              # Uploaded receipt images
├── labels/              # Ground truth JSON (code → qty mapping)
├── profiles/            # Learned receipt templates
├── results/             # Benchmark results + confusion matrix + error patterns
├── augmented/           # Augmented training images
├── learned_rules.json   # Auto-generated substitution rules (loaded by parser)
├── training_sessions.json  # Session metadata
├── correction_log.json  # Human correction history
└── labels_template.json # Template for labeling
```

---

## Frontend (`app/static/`)

**`index.html` (954L)** — Multi-tab SPA: Scan | Receipts | Catalog | Training. Camera Scanner overlay with `<video>` viewfinder + canvas capture. Quick stats bar. Editable results table (code / name / qty / confidence / delete).

**`styles.css` (2991L)** — CSS custom properties: `--primary:#4F6BF6`, `--accent:#10B981`, 5 shadow levels, spring easing. Glassmorphic header, skeleton loading, toast notifications.

**`app.js` (3985L)** key behaviors:
- Client compress: resize >1800px → JPEG 0.88 before upload
- Camera: `getUserMedia()` → `canvas.toBlob()` → `processFile()`
- Clipboard paste: `document.addEventListener('paste')` (images only, scan tab)
- Keyboard: `1/2/3` tabs, `N` new scan, `C` camera, `Escape` close
- Auto-fill: typed product code → lookup `catalogCache` → fill name (green tint)
- Dashboard refresh every 30s; `perfState.processingTimes` keeps last 20 entries
- Batch mode toggle with beforeunload warning if unsaved results exist
- Training UI: upload labeled images, run benchmarks, view optimization results

---

## Startup Lifecycle (`main.py` — 249L, uses modern `@asynccontextmanager` lifespan)

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

## OCR Deep Audit & Optimization History

### v2.0.0 — Same-Receipt-Type Optimization (March 2026)

**🏆 OVERALL: 91/100 (Grade A)** — up from 81/100 (Grade B)

| Category | Score | Grade |
|----------|-------|-------|
| Synthetic Accuracy | 99/100 | A+ |
| Real-World Quality | 95/100 | A |
| Processing Speed | 53/100 | D |
| Robustness | 99/100 | A+ |

**Current Accuracy Benchmarks:**

| Test Suite | Codes | Qty | Total | Details |
|------------|-------|-----|-------|---------|
| **5 Original Receipts** | 25/25 (100%) | 25/25 (100%) | 25/25 (100%) | All perfect including dark_ink TEW1 |
| **10 Edge Cases** | 37/37 (100%) | 37/37 (100%) | 8/9 (89%) | 1 messy-style total not read |
| **8 Real-World Images** | 40/40 (100%) | — | 40/40 (100%) | 100% qty sanity, 90% exact match |
| **Deep Test (5 img)** | 25/25 (100%) | 25/25 (100%) | 25/25 (100%) | Math verification all pass |

### v2.0.0 Optimization Changes

**Speed optimizations:**
- `OCR_CANVAS_SIZE` 1536→1280 (faster pixel processing)
- `OCR_MAG_RATIO` 2.0→1.8 (~15% speed gain)
- `OCR_SMART_PASS_THRESHOLD` 5→3 (skip 2nd pass once 3+ items found)
- Deskew threshold 0.5°→1.5° (skip minor rotations on well-aligned photos)
- `extract_text_fast()` canvas 960→1024, mag_ratio 1.2→1.5 (stronger first pass reduces need for 2nd)

**Accuracy optimizations:**
- Qty sanity threshold 500→100 (catches VWX qty=240 artefact)
- 2-col qty clamp 99→50 (tighter for same-receipt-type scanning)
- Dense receipt detection threshold 8→6 items, gap factor 0.80→0.70
- `width_ths` 0.8→0.7 (keeps alphanumeric codes TEW1/PEPW10 together)
- Smart-skip serial pipeline: items ≥ threshold AND confidence ≥ 0.55 to skip 2nd pass
- Edge case image generation: increased canvas + spacing for dense 8-item receipts

### v1.x Optimization History (Sessions 44-49)

**P0 — Config Tuning:**
- `IMAGE_MAX_DIMENSION` 1280→1800, `OCR_MIN_SIZE` 20→10
- `adjust_contrast` 0.7→0.9 (preserves faded ink)
- `FUZZY_MATCH_CUTOFF` 0.6→0.72 (tighter matching reduces phantom codes)

**P1 — Preprocessor & Parser:**
- Morphological closing now conditional (only for blurry images)
- Shadow normalization with bg_std > 15 guard (skips uniform illumination)
- Contrast stretch for low-contrast images (percentile-based)
- Code reassembly in `_clean_ocr_text`: PEPW + 20 → PEPW20 via catalog lookup
- Pipe splitting: `|` treated as line separator

**P2 — Parser Intelligence:**
- **Rotation-resistant line grouping**: right-column digits use 1.25× Y-threshold, computed against left-column code positions only
- Position-aware overlap scoring with threshold 0.7
- `_merge_local_passes` Y-bucket widened to 60px

**P3 — Edge Cases:**
- `_CODE` regex: min 3 chars for pure alpha (prevents "TH", "IN" matches)
- Trailing O/I ambiguity resolution in `_map_product_code`
- Duplicate code ambiguity resolver (PEPW1O→PEPW10 when PEPW1 also exists)
- CODE+QTY reassembly check in `_parse_line` (PEPW + 20 → PEPW20)

**P4 — Qty & Total Fixes:**
- Split-number collapse guarded (max 2 iterations), catalog-price guard, 2-col qty cap
- Skip patterns expanded, HEADER_WORDS expanded, fuzzy guard tightened
- Cross-line total detection for both qty and grand total
- OCR-garbled total variants, grand total vs qty total separation
- Backward-compatible alias keys in receipt_service

**P5 — Hybrid Engine Merge Fix:**
- Short-digit Y-distance dedup: 1-2 digit numbers use raw Y-distance (35px threshold)
- Position-based echo dedup: same (x,y) within 30×15px → keep best confidence

### Smart OCR Deep Testing (v2.1 — March 2026)

**7 bugs found and fixed** via comprehensive edge-case testing:

| # | File | Bug | Impact |
|---|------|-----|--------|
| 1 | `dedup_service.py` | Empty-code fingerprint returned fixed SHA hash | False duplicate collisions |
| 2 | `quality_scorer.py` | Missing `match_type` inflated catalog match score | Incorrect quality grades |
| 3 | `validators.py` | Rule 4 anomaly detection gated on catalog | Anomaly checks silently skipped |
| 4 | `quality_scorer.py` | `sharpness=0` displayed as `None` (falsy check) | Incorrect metadata display |
| 5 | `dedup_service.py` | SQL NULL `image_hash` not handled | Potential crash on DB read |
| 6 | `dedup_service.py` | `None` code crashed `.upper()` | Runtime AttributeError |
| 7 | `quality_scorer.py` | `None` confidence crashed `> 0` comparison | Runtime TypeError |

**Test infrastructure fix:** DB singleton shutdown issue — `test_api.py`'s TestClient lifespan calls `db.shutdown()` on the global singleton, killing the pool for subsequent test files. Fixed by having Smart OCR test classes instantiate fresh `Database()` instances instead of using the global `db` singleton.

### Remaining Limitations (EasyOCR on CPU)
- OCR O/1 confusion: "PEPW1" reads as "PEPW1O" (indistinguishable from "PEPW10") — handled by ambiguous_oi resolver
- Speed bottleneck: 93% of scan time is EasyOCR CRAFT + CRNN on CPU (~14s avg). GPU would cut to ~2-3s.
- 1 messy-style edge case total not read (high jitter + rotation on text)
- Azure Document Intelligence would significantly improve accuracy + speed for production use

---

## Test Suite Structure

**314 tests passing (283 unit + 31 integration) · 73% code coverage · 15+ test files** (threshold: 70%)

### Unit / Module Tests
| Test | Lines | Tests | Coverage |
|------|------:|------:|----------|
| `test_app.py` | 201 | 17 | App startup, configuration, middleware |
| `test_services.py` | 297 | 35 | CorrectionService (100%) + DedupService (100%) |
| `test_infrastructure.py` | 432 | 49 | logging_config (80%), tracing, metrics (100%), json_logging (85%), websocket (97%) |
| `test_middleware_and_db.py` | 450 | 48 | RateLimiter, SecurityHeaders, RateLimit, APIKey, DevTunnelCORS middlewares (96%) + DB (82%) |
| `test_parser_internals.py` | 365 | 73 | 15+ parser internal helpers (61%) — qty extraction, code matching, dedup, date/store |
| `test_observability.py` | 307 | 37 | Observability manager, tracing, metrics |
| `test_smart_ocr.py` | 702 | 67 | Dedup, quality scoring, validation, correction feedback, date/store extraction |
| `test_smart_ocr_edge_cases.py` | 991 | 103 | 9 edge-case classes: dedup, scoring, validation, correction, date/store, DB, wiring, endpoints |
| `test_preprocessing.py` | 215 | — | Image preprocessor pipeline |
| `test_training.py` | 478 | — | Training system (benchmark, optimizer, data manager, template learner) |
| `test_trainer.py` | 482 | 43 | Real-world trainer (scan, correct, error mining, confusion matrix, learned rules, augmentation) |
| `test_api.py` | 157 | — | API endpoint unit tests |
| `test_db_production.py` | 488 | 46 | Database operations, migrations, backup |
| `test_azure_integration.py` | 348 | 31 | Azure OCR engine integration |
| `test_accuracy.py` | 246 | — | OCR accuracy metrics |

### Smart OCR Edge-Case Tests (`test_smart_ocr_edge_cases.py` — 991L, 103 tests)

| Class | Tests | Coverage |
|-------|------:|----------|
| `TestDedupEdgeCases` | 18 | null hashes, empty/None codes, whitespace, case insensitivity, threshold boundaries, corrupted images |
| `TestQualityScorerEdgeCases` | 16 | None metadata, confidence boundaries, grade boundaries (90/75/60), score cap at 100, brightness bands |
| `TestValidatorEdgeCases` | 18 | empty items, anomaly without catalog, price threshold boundaries, tolerance math, missing code key |
| `TestCorrectionEdgeCases` | 10 | empty codes, case-only changes, float noise, DB errors, concurrent 20-thread access |
| `TestDateExtractionEdgeCases` | 16 | embedded dates, keyword priority, Feb 29/30, year boundaries, 2-digit years, month names |
| `TestStoreExtractionEdgeCases` | 10 | 200-char truncation, separator lines, Receipt/Invoice skipped, noise-only returns None |
| `TestDatabaseEdgeCases` | 11 | SQL injection prevention, empty kwargs, negative IDs, min_count=0, HAVING cnt ≥ 2 |
| `TestReceiptServiceWiring` | 2 | correction recording on update, no-correction on no-change |
| `TestSmartOCREndpoints` | 2 | /api/corrections and /api/item-stats response structure |

### E2E Tests
| Test | Lines | Purpose |
|------|------:|---------|
| `test_realworld_audit.py` | 497 | **Full 5-section audit** (23 images, weighted scoring → 91/100 Grade A) |
| `test_all_flows.py` | 411 | Complete API flow tests with embedded server |
| `test_new_samples.py` | 273 | 8 real-world image validation |
| `test_edge_cases.py` | 102 | 10 edge case images (dense, single-item, large qty, etc.) |
| `run_deep_test.py` | 96 | 5-image deep test with math/total verification |
| `test_ocr_accuracy.py` | 110 | 5-image accuracy benchmark |

---

## Known Behaviors / Edge Cases

- First run: ~500MB EasyOCR model download. Subsequent runs use `models/` cache.
- `AZURE_DOC_INTEL_AVAILABLE` set at import time — credential changes need server restart.
- `receipt-then-read` strategy can consume **2 Azure pages** per scan — avoid as default.
- Budget pacing warns but does **not** block — use daily/monthly limits for hard stops.
- `data/` dir is lazy-created; `data/image_cache.json` starts fresh if corrupted (silent).
- Receipt number `REC-YYYYMMDD-HHMMSS-<uuid5>` — UUID suffix prevents same-second collision.
- `HYBRID_CROSS_VERIFY=True` doubles Azure cost — only enable for accuracy benchmarking.
- Database auto-redirects to `%LOCALAPPDATA%/ReceiptScanner/` when project is in OneDrive/Dropbox (SQLite + cloud sync = corruption).
- Images saved 3× per scan: raw upload + copy + processed. Both upload + export dirs cleaned at startup (>7 days).
- `/uploads/` and `/exports/` are NOT mounted as static directories — served via route-level endpoints with filename validation and extension allowlisting.
- SQLite daily backup runs automatically before first write of each day (backups pruned after 7 days).
- Schema migrations tracked in `schema_migrations` table — never run twice. New migration = add function + tuple to MIGRATIONS list.
- PostgreSQL backend is a drop-in swap: `DB_BACKEND=postgresql` — same data shapes, zero service/route changes.
- `db.shutdown()` must be called on app exit to close pooled connections (handled by lifespan handler).
- Dashboard endpoint runs 3 DB queries in parallel via `asyncio.gather()` for 2-3× speedup.
- Docker deployment available via `Dockerfile` + `docker-compose.yml`.

---

## Quick Reference

```
run.py           → python run.py                       (server on :8000)
tests            → python -m pytest tests/
e2e              → python tests/e2e/test_all_flows.py
ocr accuracy     → python tests/integration/test_ocr_accuracy.py   (5-image accuracy benchmark)
edge cases       → python tests/e2e/test_edge_cases.py             (10-image edge case regression)
deep test        → python tests/e2e/run_deep_test.py               (5-image deep test with math/total)
full audit       → python tests/e2e/test_realworld_audit.py        (23-image comprehensive audit)
new samples      → python tests/e2e/test_new_samples.py            (8-image real-world test)
smart ocr        → python -m pytest tests/test_smart_ocr.py        (67 tests: dedup, quality, validation)
edge cases       → python -m pytest tests/test_smart_ocr_edge_cases.py  (103 tests: deep edge cases)
all unit tests   → python -m pytest tests/test_app.py tests/test_api.py tests/test_preprocessing.py tests/test_training.py tests/test_smart_ocr.py tests/test_smart_ocr_edge_cases.py -x -q  (283 tests)
benchmark        → python scripts/dev/benchmark_pipeline.py
gen receipts     → python scripts/generators/generate_test_receipts.py
gen edge cases   → python scripts/generators/generate_edge_case_receipts.py
train            → python scripts/train.py
trainer CLI      → python scripts/trainer.py                    (interactive real-world training)
trainer scan     → python scripts/trainer.py scan <image>       (scan + correct workflow)
trainer learn    → python scripts/trainer.py auto-improve       (mine errors → generate rules)
```

**Singletons:** `db` (via `get_database()`), `product_service`, `receipt_service`, `excel_service` = eager at import.  
**Lazy singletons:** `get_ocr_engine()`, `get_azure_engine()`, `get_hybrid_engine()`, `get_usage_tracker()`, `get_image_cache()`, `get_total_verifier()`, `get_obs_manager()` = created on first call.

**Free tier math:** 500 pages/month ÷ 22 work days ≈ 22 pages/day. With local-first skip (conf≥0.85 AND detects≥4 AND catalog≥30%) ~40-60% of scans skip Azure → effective ~35-50 scans/day.
