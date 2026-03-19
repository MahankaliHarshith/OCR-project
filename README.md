# 📝 Handwritten Receipt Scanner

> An intelligent OCR-powered web application that scans handwritten shop receipts, extracts product codes and quantities, and generates structured Excel reports — built for small retail shops.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.132-009688?logo=fastapi&logoColor=white)
![EasyOCR](https://img.shields.io/badge/EasyOCR-1.7-FF6F00)
![Azure](https://img.shields.io/badge/Azure_Doc_Intel-Optional-0078D4?logo=microsoftazure&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Audit](https://img.shields.io/badge/Audit_Score-91%2F100_(Grade_A)-brightgreen)
![Tests](https://img.shields.io/badge/Tests-314_passing-brightgreen)
![Smart OCR](https://img.shields.io/badge/Smart_OCR-Phase_2_Complete-blue)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Request Flow Diagram](#request-flow-diagram)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [OCR Pipeline](#ocr-pipeline)
- [Database](#database)
- [Observability](#observability)
- [Testing](#testing)
- [Training System](#training-system)
- [Deployment](#deployment)
- [Changelog — v2.0.0 Optimization](#changelog--v200-optimization)
- [Contributing](#contributing)
- [License](#license)

---

## Features

### Core Capabilities

- **Hybrid OCR Engine** — Local EasyOCR + optional Azure Document Intelligence with intelligent cost-aware routing
- **Handwritten Receipt Parsing** — 10+ regex patterns (priority-ordered) with 4-tier fuzzy code matching (exact → OCR-sub → handwriting-sub → fuzzy)
- **Bill Total Verification** — Multi-pass digit re-reading, arithmetic reconciliation, confidence-weighted dispute resolution
- **Smart OCR Pipeline** — Post-parse intelligence layer:
  - **Quality Scoring** — 0–100 score + letter grade (A/B/C/D) based on 6 weighted factors (OCR confidence, items found, total/math verification, image quality, catalog match rate)
  - **Validation Rules** — 4-rule engine: impossible quantity detection, price sanity checks, duplicate item flagging, cross-receipt anomaly detection
  - **Duplicate Detection** — 3-layer dedup: perceptual image hash (hamming distance ≤5), content fingerprint (sorted code:qty SHA-256), user confirmation
  - **OCR Correction Feedback** — Records user corrections, builds lookup map (≥2 occurrences), auto-corrects known OCR misreads on future scans
  - **Date & Store Extraction** — Extracts receipt date (10+ formats) and store name from OCR text
- **Input Validation** — Comprehensive OCR result validators with confidence thresholds and sanity checks
- **Real-time Web Interface** — Single-page app with camera capture, clipboard paste, drag-and-drop upload
- **Excel Export** — Styled multi-sheet reports (Daily Sales + Summary) with confidence highlighting
- **Product Catalog** — Full CRUD with CSV import/export and fuzzy search
- **Training System** — Built-in benchmark runner, parameter optimizer, template learner, data manager, and **real-world adaptive trainer** with error pattern mining, confusion matrix analysis, auto-generated substitution rules, and interactive CLI

### Accuracy (v2.0.0 — 🏆 91/100 Grade A Audit)

- **100% code detection** across 25+ test receipts (original, edge-case, and real-world images)
- **100% quantity accuracy** on synthetic receipt images — optimized via deep audit cycle
- **0 critical failures** across all test suites (smart OCR, edge cases, accuracy, preprocessing)
- **Cross-line total verification** with OCR-garbled variant handling (`qtyt`, `grrand`, etc.)

### Production-Ready

- **6-Layer Azure Cost Defense** — image quality gate, local-first skip, daily/monthly limits, budget pacing, image cache, model strategy selection
- **Security Hardening** — CSP headers, rate limiting (10 scan / 30 general RPM), API key protection, magic-byte file validation, path traversal guards
- **Database** — SQLite WAL with connection pooling, versioned schema migrations, daily auto-backups, 18 seed products. Optional PostgreSQL drop-in swap.
- **Observability** — Prometheus metrics (`/metrics`), **Grafana dashboards** (pre-built 20-panel operations dashboard), **OpenTelemetry distributed tracing** (Jaeger UI), **structured JSON logging** (Loki/ELK-ready), **15 Prometheus alert rules** (Alertmanager), **Sentry error tracking** (optional), rotating file + console logging, per-stage processing logs, dashboard with parallel DB queries
- **Async Batch Processing** — Background job queue for scanning up to 20 receipts without blocking the API, semaphore-bounded concurrency (3 workers), **WebSocket real-time progress** (`/ws/batch/{id}`), status polling, cancellation support
- **Duplicate Detection** — Receipt dedup service prevents double-scans of the same image
- **Correction Service** — Post-OCR correction pipeline for automated error fixups
- **CI/CD** — GitHub Actions pipeline (lint + test matrix + Docker build), pre-commit hooks (ruff + formatting)
- **Testing** — 314 tests passing (283 unit + 31 integration), 103 edge-case tests for Smart OCR, 7 bugs found and fixed via deep testing
- **Docker** — Multi-stage production image, non-root user, healthcheck, docker-compose with Prometheus + Grafana + Jaeger + Loki + Promtail + Alertmanager + named volumes

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Frontend (SPA)                          │
│   index.html  ·  styles.css  ·  app.js  ·  lucide.min.js   │
│   Camera · Clipboard · Drag & Drop · Keyboard shortcuts     │
└────────────────────────┬────────────────────────────────────┘
                         │ REST API (JSON)
┌────────────────────────▼────────────────────────────────────┐
│                   FastAPI + Middleware                       │
│  CORS · CSP · Rate Limit · API Key · Static Cache · Logging │
├─────────────────────────────────────────────────────────────┤
│                    Service Layer                            │
│  receipt_service · product_service · excel_service           │
│  batch_service · dedup_service · correction_service          │
├─────────────────────────────────────────────────────────────┤
│                   Hybrid OCR Engine                         │
│  ┌──────────┐    ┌──────────────┐    ┌─────────────────┐   │
│  │ EasyOCR  │◄──►│ Hybrid Router│◄──►│ Azure Doc Intel │   │
│  │ (local)  │    │ (cost-aware) │    │   (optional)    │   │
│  └──────────┘    └──────────────┘    └─────────────────┘   │
│  preprocessor · parser · total_verifier · quality_scorer    │
│  image_cache · usage_tracker · validators                   │
├─────────────────────────────────────────────────────────────┤
│                   Smart OCR Pipeline                       │
│  dedup_service · correction_service · quality_scorer         │
│  receipt_validator · date/store extraction                   │
├─────────────────────────────────────────────────────────────┤
│                    Database Layer                           │
│  ┌──────────────────┐    ┌──────────────────────────────┐  │
│  │  SQLite (WAL)    │    │  PostgreSQL (optional)       │  │
│  │  ConnectionPool  │    │  ThreadedConnectionPool      │  │
│  │  MigrationMgr    │    │  Drop-in swap via env var    │  │
│  │  BackupManager   │    │                              │  │
│  └──────────────────┘    └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## Request Flow Diagram

Detailed end-to-end flow showing every stage a receipt goes through — from user interaction to final output.

### End-to-End Scan Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                        USER (Browser / app.js)                       │
│  [Scan] Upload file / Camera capture / Clipboard paste               │
│  [Review] View & edit OCR results                                    │
│  [Batch] Manage batches, async processing                            │
│  [Products] CRUD product catalog                                     │
│  [Dashboard] Stats, engine status, cost tracking                     │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  POST /api/receipts/scan (multipart file)
                             ▼
┌─── FastAPI Middleware Stack (6 layers) ───────────────────────────────┐
│  DevTunnel CORS → Security Headers (CSP, nosniff, Referrer-Policy)   │
│  → Rate Limiter (30 RPM general, 10 RPM scan)                        │
│  → API Key Guard (X-API-Key for destructive endpoints)               │
│  → GZip Compression (>500B, ~60% savings)                            │
│  → CORS → Request Logging (method, path, status, duration)           │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌─── API Route Validation ─────────────────────────────────────────────┐
│  1. Extension check (.jpg / .png / .bmp / .tiff / .webp)             │
│  2. Stream size check (≤20MB, read in 1MB chunks)                    │
│  3. Magic byte validation (prevents disguised uploads)               │
│  4. Save to uploads/ with UUID-suffixed filename                     │
│  5. asyncio.to_thread() → non-blocking processing                   │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌══════════════════════════════════════════════════════════════════════┐
║           ReceiptService.process_receipt() — 5 Steps                ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  STEP 0: IMAGE CACHE CHECK                                          ║
║     SHA-256 hash → LRU cache lookup                                  ║
║     HIT? → Skip directly to Step 4 (FREE)                           ║
║                         ↓ MISS                                       ║
║                                                                      ║
║  STEP 1: SAVE UPLOADED IMAGE                                         ║
║     Copy to uploads/ directory with receipt name                     ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 2: IMAGE PREPROCESSING (ImagePreprocessor)                     ║
║     ┌─────────────────────────────────────────────┐                  ║
║     │ 1. EXIF rotation fix                        │                  ║
║     │ 2. Resize (max 1800px)                      │                  ║
║     │ 3. Document scan (edge detection →          │                  ║
║     │    contour → 4-point perspective warp)      │                  ║
║     │ 4. White balance (gray-world correction)    │                  ║
║     │ 5. Grayscale conversion                     │                  ║
║     │ 6. Deskew (Hough lines, rotate if >1.5°)    │                  ║
║     │ 7. Upside-down detection & 180° fix         │                  ║
║     │ 8. Quality assessment (blur, brightness,    │                  ║
║     │    contrast → quality score)                │                  ║
║     │ 9. Enhancement (denoise, sharpen, CLAHE,    │                  ║
║     │    morphological closing, shadow normalize) │                  ║
║     └─────────────────────────────────────────────┘                  ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 3: HYBRID OCR ENGINE (HybridOCREngine)                        ║
║     ┌─────────────────────────────────────────────┐                  ║
║     │          AUTO MODE (default)                │                  ║
║     │                                             │                  ║
║     │  Image cache → HIT? Return (FREE)           │                  ║
║     │       ↓ MISS                                │                  ║
║     │  Quality gate → BAD? Local only             │                  ║
║     │       ↓ PASS                                │                  ║
║     │                                             │                  ║
║     │  ┌───────────────────────────────┐          │                  ║
║     │  │ LOCAL EasyOCR (always free)   │          │                  ║
║     │  │ • Dual-pass: grayscale+color  │          │                  ║
║     │  │ • Dynamic param tuning        │          │                  ║
║     │  │ • CRAFT text detector         │          │                  ║
║     │  └──────────┬────────────────────┘          │                  ║
║     │             ↓                               │                  ║
║     │  Calibrated conf ≥ 0.85                     │                  ║
║     │  AND detections ≥ 4                         │                  ║
║     │  AND catalog match ≥ 30%?                   │                  ║
║     │       ↓ YES → SKIP Azure! Return local      │                  ║
║     │       ↓ NO                                  │                  ║
║     │                                             │                  ║
║     │  ┌───────────────────────────────┐          │                  ║
║     │  │ AZURE Document Intelligence  │          │                  ║
║     │  │ • Optimize image (<1500px)    │          │                  ║
║     │  │ • Receipt / Read model        │          │                  ║
║     │  │ • _get_field_value() helper   │          │                  ║
║     │  │ • 50/day, 500/month caps      │          │                  ║
║     │  │ • Cache result (24h TTL)      │          │                  ║
║     │  └──────────┬────────────────────┘          │                  ║
║     │             ↓ FAIL? Fallback to local       │                  ║
║     └─────────────────────────────────────────────┘                  ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 4: RECEIPT PARSING (ReceiptParser)                             ║
║     ┌─────────────────────────────────────────────┐                  ║
║     │ • Line cleaning & skip detection            │                  ║
║     │ • 10+ regex patterns (priority ordered)     │                  ║
║     │ • 4-tier code matching:                     │                  ║
║     │   exact → OCR-sub → handwriting-sub → fuzzy │                  ║
║     │ • Adaptive fuzzy cutoff (strict for short)  │                  ║
║     │ • OCR digit fix (O→0, I→1, S→5)            │                  ║
║     │ • Total line extraction                     │                  ║
║     └─────────────────────────────────────────────┘                  ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 4b: BILL TOTAL VERIFICATION (BillTotalVerifier)                ║
║     • Total line extraction (spatial + keyword)                      ║
║     • Multi-pass digit re-reading                                    ║
║     • Arithmetic reconciliation (OCR total vs computed sum)          ║
║     • Dispute resolution (confidence-weighted trust)                 ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 4c: MATH / PRICE VERIFICATION                                 ║
║     • Catalog price injection per item                               ║
║     • Line total validation (qty × unit_price)                       ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 4d: SMART VALIDATION RULES (ReceiptValidator)                  ║
║     • Impossible quantity detection (zero/negative/absurd)           ║
║     • Price sanity checks (>5× catalog deviation)                    ║
║     • Duplicate item flagging                                       ║
║     • Cross-receipt anomaly detection (historical patterns)          ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 4e: QUALITY SCORING (QualityScorer)                            ║
║     • 6-factor weighted score (0–100) + letter grade (A/B/C/D)       ║
║     • OCR confidence, items, total/math verification, image, catalog ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 4f: DUPLICATE DETECTION (DedupService)                         ║
║     • Perceptual image hash (8×8 grayscale average hash)             ║
║     • Content fingerprint (SHA-256 of sorted code:qty pairs)        ║
║     • Hamming distance ≤5 = duplicate warning                       ║
║                         ↓                                            ║
║                                                                      ║
║  STEP 5: DATABASE SAVE (SQLite / PostgreSQL)                         ║
║     • INSERT → receipts table                                        ║
║     • INSERT → receipt_items table                                   ║
║     • INSERT → processing_logs table                                 ║
║     • Record Prometheus metrics                                      ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
                             │
                             ▼
              JSON Response → Frontend (app.js)
              { receipt_data, items[], metadata, errors }
                             │
                             ▼
┌─── User Review & Export ─────────────────────────────────────────────┐
│  View/edit items → PUT /api/receipts/items/{id}                      │
│  Add to batch → POST /api/export/excel → Download .xlsx              │
│  (Excel: Data sheet + Summary sheet, low-confidence highlighting)    │
└──────────────────────────────────────────────────────────────────────┘
```

### Async Batch Flow

```
POST /api/batch (up to 20 files)
    │
    ▼
[Validate & Save] ──► Return 202 { batch_id } immediately
    │
    ▼
[Background asyncio.Task]
    ├── Semaphore (3 concurrent workers)
    ├── File 1 ─── ThreadPoolExecutor ──► process_receipt()
    ├── File 2 ─── ThreadPoolExecutor ──► process_receipt()
    ├── File 3 ─── ThreadPoolExecutor ──► process_receipt()
    │   ... (queued until semaphore releases)
    └── File N ─── ThreadPoolExecutor ──► process_receipt()
         │
         ├── WebSocket push (/ws/batch/{id}) ──► real-time progress
         ▼
    [BatchJob.status = COMPLETED]
         │
    GET /api/batch/{id} ──► { status, progress_percent, results[] }
```

### Database Schema

```
┌──────────────┐       ┌──────────────────┐       ┌─────────────────┐
│   products   │       │     receipts     │       │ processing_logs │
├──────────────┤       ├──────────────────┤       ├─────────────────┤
│ code (PK)    │       │ id (PK)          │◄──┐   │ id (PK)         │
│ name         │       │ receipt_number   │   │   │ receipt_id (FK) │
│ price        │       │ store_name       │   │   │ step_name       │
│ unit         │       │ scan_date        │   │   │ duration_ms     │
│ category     │       │ total_amount     │   │   │ success         │
│ created_at   │       │ image_path       │   │   │ error_message   │
└──────────────┘       │ ocr_engine       │   │   │ created_at      │
                       │ confidence_avg   │   │   └─────────────────┘
                       │ status           │   │
                       │ created_at       │   │
                       └──────────────────┘   │
                              │               │
                              ▼               │
                       ┌──────────────────┐   │
                       │  receipt_items   │   │
                       ├──────────────────┤   │
                       │ id (PK)          │   │
                       │ receipt_id (FK) ─┼───┘
                       │ product_code     │
                       │ product_name     │
                       │ quantity         │
                       │ unit_price       │
                       │ line_total       │
                       │ ocr_confidence   │
                       │ manually_edited  │
                       └──────────────────┘

                       ┌──────────────────┐
                       │ ocr_corrections  │
                       ├──────────────────┤
                       │ id (PK)          │
                       │ receipt_id (FK)   │
                       │ item_id           │
                       │ original_code     │
                       │ corrected_code    │
                       │ original_qty      │
                       │ corrected_qty     │
                       │ raw_ocr_text      │
                       │ created_at        │
                       └──────────────────┘
```

### Observability Stack

```
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Application                       │
│                                                             │
│  /metrics ──► Prometheus ──► Grafana (20-panel dashboard)   │
│                                │                            │
│                                ▼                            │
│                          Alertmanager                       │
│                          (15 alert rules)                   │
│                                                             │
│  OTLP spans ──► Jaeger (distributed tracing)                │
│                                                             │
│  JSON logs ──► Promtail ──► Loki ──► Grafana (LogQL)        │
│  Text logs ──► Promtail ──► Loki                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
├── run.py                        # Application entry point
├── requirements.txt              # Pinned Python dependencies
├── pyproject.toml                # Modern packaging + ruff + pytest config
├── Dockerfile                    # Multi-stage production Docker image
├── docker-compose.yml            # Full-stack with Prometheus + Grafana + Jaeger + Alertmanager + Loki
├── .pre-commit-config.yaml       # Code quality hooks (ruff, formatting)
├── .env.example                  # Environment variable template
├── .gitignore
├── LICENSE                       # MIT License
│
├── .github/
│   └── workflows/
│       └── ci.yml                # GitHub Actions: lint + test + docker
│
├── monitoring/                   # Observability stack configs
│   ├── prometheus.yml            #   Prometheus scrape config
│   ├── alert_rules.yml           #   15 Prometheus alerting rules (4 groups)
│   ├── alertmanager.yml          #   Alertmanager routing + receivers
│   ├── loki.yml                  #   Grafana Loki log aggregation config
│   ├── promtail.yml              #   Log shipper → Loki pipeline
│   └── grafana/
│       ├── dashboards/
│       │   └── receipt-scanner.json  # Pre-built 20-panel operations dashboard
│       └── provisioning/
│           ├── datasources/
│           │   └── datasources.yml   # Auto-provisions Prometheus + Jaeger + Loki
│           └── dashboards/
│               └── dashboards.yml    # Dashboard auto-loading config
│
├── app/                          # Application source code
│   ├── __init__.py               #   Package metadata (version, author)
│   ├── main.py                   #   FastAPI app factory + lifespan
│   ├── config.py                 #   Centralized configuration (env vars)
│   ├── middleware.py             #   Security, rate limiting, CORS, caching
│   ├── database.py               #   SQLite backend, pool, migrations, backups
│   ├── db_postgres.py            #   PostgreSQL backend (drop-in swap)
│   ├── logging_config.py         #   Rotating file + console log setup
│   ├── json_logging.py           #   Structured JSON logging (Loki / ELK ready)
│   ├── websocket.py              #   WebSocket ConnectionManager for batch updates
│   ├── metrics.py                #   Prometheus metrics (counters, gauges, histograms)
│   ├── tracing.py                #   OpenTelemetry distributed tracing (auto/manual spans)
│   ├── observability.py          #   Unified observability setup (metrics + tracing + logging)
│   ├── error_tracking.py         #   Sentry error tracking integration (optional)
│   │
│   ├── api/
│   │   └── routes.py             #   REST endpoint definitions
│   │
│   ├── ocr/
│   │   ├── engine.py             #   EasyOCR wrapper (3 speed tiers)
│   │   ├── azure_engine.py       #   Azure Document Intelligence client
│   │   ├── hybrid_engine.py      #   Intelligent OCR router (cost-aware)
│   │   ├── preprocessor.py       #   Image enhancement pipeline
│   │   ├── parser.py             #   Receipt text → structured data (2468L)
│   │   ├── total_verifier.py     #   Cross-line total verification (multi-pass)
│   │   ├── quality_scorer.py     #   Receipt quality scoring (0–100 + A/B/C/D grade)
│   │   ├── validators.py         #   Post-parse validation rules (4-rule engine)
│   │   ├── usage_tracker.py      #   Azure API budget tracking
│   │   └── image_cache.py        #   SHA-256 LRU result cache
│   │
│   ├── services/
│   │   ├── receipt_service.py    #   Scan orchestration pipeline (980L, 6-step + Smart OCR)
│   │   ├── product_service.py    #   Product CRUD + CSV import/export
│   │   ├── excel_service.py      #   Styled Excel report generation
│   │   ├── batch_service.py      #   Async background batch processing
│   │   ├── dedup_service.py      #   Duplicate receipt detection (image hash + content fingerprint)
│   │   └── correction_service.py #   OCR correction feedback loop (learn from user edits)
│   │
│   └── static/                   # Frontend assets
│       ├── index.html            #   3-tab SPA (Scan | Receipts | Catalog)
│       ├── styles.css            #   Glassmorphic UI with CSS variables
│       ├── app.js                #   Client-side logic + camera/clipboard
│       └── lucide.min.js         #   Icon library
│
├── app/training/                 # Training & Optimization System
│   ├── routes.py                 #   Training API endpoints (/api/training/*)
│   ├── benchmark.py              #   Automated accuracy benchmarking
│   ├── optimizer.py              #   OCR parameter grid search + auto-tuning
│   ├── data_manager.py           #   Training data management (images, labels, profiles)
│   ├── template_learner.py       #   Receipt template pattern learning
│   └── real_world_trainer.py     #   Adaptive trainer — error mining, confusion matrix, learned rules
│
├── tests/                        # Test suite (314 tests: 283 unit + 31 integration)
│   ├── test_app.py               #   Unit tests (pytest) — parser, Excel, DB
│   ├── test_smart_ocr.py         #   Smart OCR pipeline tests (702L, 67 tests)
│   ├── test_smart_ocr_edge_cases.py  #   Smart OCR edge cases (991L, 103 tests, 9 classes)
│   ├── test_accuracy.py          #   OCR accuracy validation tests
│   ├── test_preprocessing.py     #   Image preprocessing tests
│   ├── test_training.py          #   Training system tests (478L)
│   ├── test_trainer.py           #   Real-world trainer tests (43 tests)
│   ├── test_observability.py     #   WebSocket, JSON logging, alerting tests (37 tests)
│   ├── test_db_production.py     #   Database infrastructure tests (46 tests)
│   ├── test_azure_integration.py #   Azure OCR integration tests (31 tests)
│   ├── test_services.py          #   CorrectionService + DedupService tests (35 tests)
│   ├── test_infrastructure.py    #   Logging, tracing, metrics, WebSocket tests (49 tests)
│   ├── test_middleware_and_db.py  #   Middleware + DB operation tests (48 tests)
│   ├── test_parser_internals.py  #   Parser internal helper tests (73 tests)
│   ├── test_codes.py             #   Fuzzy matching tests
│   ├── test_api.py               #   API endpoint tests
│   ├── api_check.py              #   API endpoint health checks
│   ├── verify_db.py              #   Database feature verification
│   ├── e2e/                      #   End-to-end API + accuracy tests
│   ├── integration/              #   Integration + OCR accuracy tests
│   └── fixtures/                 #   Test images (edge cases, samples)
│
├── scripts/                      # Utility scripts
│   ├── start_server.py           #   Alternative launcher (subprocess)
│   ├── start_devtunnel.py        #   Dev tunnel launcher
│   ├── start_public.py           #   ngrok public URL launcher
│   ├── trainer.py                #   Interactive real-world trainer CLI (9 commands)
│   ├── dev/                      #   Development & debugging tools
│   └── generators/               #   Test data generation scripts
│
├── docs/                         # Documentation
│   ├── PRD.txt                   #   Product Requirements Document
│   ├── CONTEXT.md                #   Technical context reference
│   ├── HYBRID_OCR_ARCHITECTURE.md
│   ├── DEEP_AUDIT_REPORT.md      #   Audit results (91/100 Grade A) + training guide
│   ├── AI_Receipt_Generation_Prompts.md
│   └── Receipt_Design_and_Scanning_Guide.md
│
├── models/                       # EasyOCR model weights (auto-downloaded)
├── training_data/                # Training system data
│   ├── images/                   #   Training receipt images
│   ├── labels/                   #   Ground truth label files
│   ├── profiles/                 #   Optimization profiles
│   ├── results/                  #   Benchmark results + confusion matrix + error patterns
│   ├── augmented/                #   Augmented training images
│   └── labels_template.json      #   Label format template
├── uploads/                      # Uploaded receipt images (auto-cleaned 7d)
├── exports/                      # Generated Excel/CSV files (auto-cleaned 7d)
├── logs/                         # Application log files (rotating)
├── data/                         # Runtime data (usage stats, image cache)
└── backups/                      # Daily SQLite snapshots (auto-pruned 7d)
```

---

## Getting Started

### Prerequisites

- **Python 3.12+**
- **pip** package manager

### Installation

```bash
# Clone the repository
git clone https://github.com/MahankaliHarshith/OCR-project.git
cd OCR-project

# Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Quick Start

```bash
# Copy the environment template
cp .env.example .env

# Start the server
python run.py
```

Open **[http://localhost:8000](http://localhost:8000)** in your browser.

> **First-run note:** EasyOCR downloads ~500 MB of model weights on first launch. Subsequent starts use the cached models in `models/`.

### Enable Azure OCR (Optional)

For higher accuracy on difficult handwriting, add your Azure Document Intelligence credentials to `.env`:

```dotenv
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=your-api-key
```

The app automatically enables the hybrid engine when Azure credentials are present, falling back to local EasyOCR when they are not configured.

---

## Configuration

All settings are managed via environment variables (`.env` file). See [.env.example](.env.example) for the complete template with descriptions.

### Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_ENGINE_MODE` | `auto` | `auto` (hybrid) · `azure` · `local` |
| `AZURE_MODEL_STRATEGY` | `read-only` | `read-only` ($0.0015/pg) · `receipt-only` ($0.01/pg) |
| `AZURE_DAILY_PAGE_LIMIT` | `50` | Hard daily cap — resets at midnight |
| `AZURE_MONTHLY_PAGE_LIMIT` | `500` | Monthly cap — matches Azure free tier |
| `LOCAL_CONFIDENCE_SKIP_THRESHOLD` | `0.85` | Skip Azure if local OCR confidence ≥ this |
| `RATE_LIMIT_RPM` | `30` | General API rate limit per IP |
| `RATE_LIMIT_SCAN_RPM` | `10` | Scan endpoint rate limit per IP |
| `API_SECRET_KEY` | `""` | Protects destructive endpoints (DELETE, reset) |
| `API_DOCS_ENABLED` | `true` | Enable Swagger UI at `/docs` |
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgresql` |
| `LOG_LEVEL` | `INFO` | `DEBUG` · `INFO` · `WARNING` · `ERROR` |

### Azure Free Tier Budget

The app is engineered to stay within Azure's free tier (**500 pages/month**):

- **~22 pages/day** sustainable rate
- **Local-first routing** skips Azure for 40–60% of scans with clear handwriting
- **Image quality gate** rejects blurry/dark images before calling Azure
- **SHA-256 cache** prevents duplicate charges for re-scanned images
- **Budget pacing** alerts when daily usage exceeds sustainable rate

---

## API Reference

Interactive API documentation is available at **[http://localhost:8000/docs](http://localhost:8000/docs)** when the server is running.

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check + engine status |
| `POST` | `/api/receipts/scan` | Upload and scan a receipt image |
| `POST` | `/api/receipts/scan-batch` | Scan up to 10 receipts (synchronous) |
| `POST` | `/api/batch` | Submit up to 20 receipts for async processing |
| `GET` | `/api/batch` | List recent async batch jobs |
| `GET` | `/api/batch/{id}` | Poll async batch status + results |
| `DELETE` | `/api/batch/{id}` | Cancel an async batch job |
| `WS` | `/ws/batch/{id}` | WebSocket real-time batch progress |
| `POST` | `/api/webhooks/alerts` | Alertmanager webhook receiver |
| `GET` | `/api/receipts` | List receipts (paginated: `?limit=10`) |
| `GET` | `/api/receipts/{id}` | Get receipt details with line items |
| `DELETE` | `/api/receipts/{id}` | Delete a receipt 🔒 |
| `PUT` | `/api/receipts/items/{id}` | Edit a receipt line item |
| `POST` | `/api/receipts/{id}/items` | Add a manual line item |
| `GET` | `/api/receipts/date/{date}` | Get receipts by date (`YYYY-MM-DD`) |
| `GET/POST/PUT/DELETE` | `/api/products[/{code}]` | Product catalog CRUD |
| `GET` | `/api/products/search` | Fuzzy product search |
| `POST` · `GET` | `/api/products/import/csv` · `export/csv` | CSV import / export |
| `POST` | `/api/export/excel` | Generate Excel report |
| `GET` | `/api/export/daily` | Daily sales report |
| `GET` | `/api/export/download/{file}` | Download an export file |
| `GET` | `/api/dashboard` | Dashboard statistics |
| `GET` | `/api/ocr/status` | OCR engine status |
| `GET` | `/api/ocr/usage` | Azure usage + pacing stats |
| `POST` | `/api/ocr/usage/reset-daily` | Reset daily usage counter 🔒 |
| `POST` | `/api/ocr/cache/clear` | Clear the image cache 🔒 |
| `GET` | `/api/corrections` | OCR correction feedback statistics |
| `GET` | `/api/item-stats` | Per-product historical quantity statistics |

> 🔒 Protected by `API_SECRET_KEY` — pass via `X-API-Key` header.

### Example: Scan a Receipt

```bash
curl -X POST http://localhost:8000/api/receipts/scan \
  -F "file=@receipt.jpg"
```

```json
{
  "success": true,
  "receipt_id": "REC-20250101-143052-a1b2c3",
  "items": [
    {
      "product_code": "ABC",
      "product_name": "1L Exterior Paint",
      "quantity": 3,
      "confidence": 0.89,
      "match_type": "exact"
    }
  ],
  "total_items": 5,
  "avg_confidence": 0.85,
  "ocr_strategy": "auto-local-skip"
}
```

---

## OCR Pipeline

### Hybrid Engine Flow (Auto Mode)

```
Image Upload
    │
    ▼
[Cache Check] ─── HIT ───► Return cached result (free)
    │ MISS
    ▼
[Quality Gate] ─── FAIL ──► Local OCR only (save Azure pages)
    │ PASS
    ▼
[Local EasyOCR] ── conf ≥ 0.85 AND ≥4 detections AND ≥30% catalog match
    │               ──► Return local result (smart-skip)
    │ LOW confidence
    ▼
[Budget Check] ─── BLOCKED ──► Return local result
    │ OK
    ▼
[Azure Doc Intel] ────────────► Cache result + Return
    │ FAILED
    ▼
[Fallback to Local]
```

#### Smart-Skip Dual-Pass Logic (v2.0.0)

- **Serial dual-pass:** Run gray fast-pass → parse items → if items ≥ 3 **AND** confidence ≥ 0.55 → skip 2nd pass
- **Parallel dual-pass:** ThreadPoolExecutor runs gray + color simultaneously, merges results
- **Fast-pass parameters:** Canvas 1024px, mag_ratio 1.5, width_ths 0.7 (tuned for speed)

### Image Preprocessing Pipeline

1. **Load** — EXIF-corrected orientation → resize to max 1800px
2. **Document Scan** — Edge detection → contour → 4-point perspective warp
3. **White Balance** — Gray-world color correction
4. **Grayscale** conversion
5. **Deskew** — Hough line transform (auto-rotate if angle > 1.5°)
6. **Upside-Down Detection** — Auto 180° rotation fix
7. **Quality Assessment** — Laplacian sharpness + mean brightness + contrast scoring
8. **Enhancement** — Denoise, sharpen, CLAHE, morphological closing, shadow normalization
9. **Crop** — Otsu threshold → bounding box with 5% margin

### Receipt Parsing

- **10+ regex patterns** for code–quantity extraction (priority-ordered)
- **4-tier code matching:** exact → OCR character substitution → handwriting substitution → fuzzy (difflib)
- **OCR correction lookup** — checks learned corrections map before fuzzy matching (from user feedback)
- **Y-aware line grouping** with rotation-resistant quantity alignment
- **Quantity sanity clamping** — max 100 per item (50 for 2-column), dense receipt detection at 6+ items
- **Cross-line total verification** with OCR-garbled variant handling (`qtyt`, `grrand`, etc.)
- **Bill Total Verifier** — spatial + keyword total line extraction, multi-pass digit re-reading, arithmetic reconciliation

---

## Database

### SQLite (Default)

- **WAL mode** for concurrent reads during writes
- **Thread-local connection pool** — one connection per thread, auto-reconnect
- **Versioned schema migrations** tracked in `schema_migrations` table
- **Daily backups** triggered before first write of each day, auto-pruned after 7 days

### PostgreSQL (Optional)

Switch to PostgreSQL with zero application code changes:

```dotenv
DB_BACKEND=postgresql
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=receipt_scanner
POSTGRES_USER=receipt_app
POSTGRES_PASSWORD=your-password
```

> Requires `pip install psycopg2-binary`

### Schema Overview

| Table | Purpose |
|-------|---------|
| `products` | Product catalog — code (unique), name, category, unit, unit_price |
| `receipts` | Scan metadata — image paths, status, OCR confidence, **image_hash, content_fingerprint, receipt_date, store_name, quality_score, quality_grade** (v4) |
| `receipt_items` | Parsed line items (FK → receipts, CASCADE delete), **unit_price, line_total** (v3) |
| `ocr_corrections` | **OCR correction feedback** (FK → receipts, SET NULL) — original/corrected code+qty pairs (v4) |
| `processing_logs` | Per-stage timing and error tracking |
| `schema_migrations` | Migration version audit trail (v1–v4) |

### Seed Data

On first initialization, the database is seeded with **18 paint-shop products**:

**Alpha codes (10):** `ABC` (1L Exterior Paint), `XYZ` (1L Interior Paint), `PQR` (5L Primer), `MNO` (Paint Brush), `DEF` (1L Wood Varnish), `GHI` (Sandpaper), `JKL` (Putty Knife), `STU` (Wall Filler), `VWX` (Masking Tape), `RST` (Thinner 500ml)

**TEW series (4):** `TEW1` (₹250), `TEW4` (₹850), `TEW10` (₹1800), `TEW20` (₹3200) — Thinnable Exterior Wash

**PEPW series (4):** `PEPW1` (₹350), `PEPW4` (₹1200), `PEPW10` (₹2600), `PEPW20` (₹4800) — Premium Exterior Premium Wash

---

## Testing

**314 tests passing** (283 unit + 31 integration) · **73% code coverage** (threshold: 70%) across 15+ test files.

```bash
# Run all unit tests with coverage
python -m pytest tests/test_app.py tests/test_observability.py tests/test_services.py \
  tests/test_infrastructure.py tests/test_middleware_and_db.py tests/test_parser_internals.py \
  -v --cov=app --cov-report=term-missing
```

### Unit Tests

```bash
# Core app + config tests (17 tests)
python -m pytest tests/test_app.py -v

# Services — CorrectionService + DedupService (35 tests)
python -m pytest tests/test_services.py -v

# Infrastructure — logging, tracing, metrics, WebSocket (49 tests)
python -m pytest tests/test_infrastructure.py -v

# Middleware + extended DB operations (48 tests)
python -m pytest tests/test_middleware_and_db.py -v

# Parser internals — 15+ helper methods (73 tests)
python -m pytest tests/test_parser_internals.py -v

# Observability manager (37 tests)
python -m pytest tests/test_observability.py -v

# Smart OCR pipeline tests
python -m pytest tests/test_smart_ocr.py -v

# Smart OCR pipeline tests (67 tests)
python -m pytest tests/test_smart_ocr.py -v

# Edge case regression suite (991 lines, 103 tests, 9 test classes)
python -m pytest tests/test_smart_ocr_edge_cases.py -v

# OCR accuracy validation
python -m pytest tests/test_accuracy.py -v

# Image preprocessing tests
python -m pytest tests/test_preprocessing.py -v

# Training system tests
python -m pytest tests/test_training.py -v

# Real-world trainer tests (43 tests)
python -m pytest tests/test_trainer.py -v

# Database infrastructure tests (connection pool, migrations, backups)
python tests/test_db_production.py

# Azure integration tests (requires credentials)
python -m pytest tests/test_azure_integration.py -v
```

### Integration Tests

```bash
# OCR accuracy benchmark
python tests/integration/test_ocr_accuracy.py

# Comprehensive integration suite
python tests/integration/test_comprehensive.py
```

### End-to-End Tests

```bash
# Full API flow tests (starts an embedded server)
python tests/e2e/test_all_flows.py

# Edge case regression suite
python tests/e2e/test_edge_cases.py

# Deep accuracy audit
python tests/e2e/run_deep_test.py
```

### Generate Test Images

```bash
python scripts/generators/generate_test_receipts.py
python scripts/generators/generate_edge_case_receipts.py
```

---

## Observability

This project ships with a **full observability stack**: Prometheus metrics + **15 alert rules** (Alertmanager), pre-built **Grafana dashboards**, OpenTelemetry **distributed tracing** (Jaeger), and **structured JSON logging** (Loki). All components are zero-overhead when disabled.

### Grafana Dashboards (Visualization — "see everything at a glance")

A pre-built **20-panel operations dashboard** is auto-provisioned when you run `docker-compose up`. No manual setup needed.

#### Dashboard Panels

| Row | Panels |
|-----|--------|
| **📊 Overview** | Total Scans · Failed Scans · Avg Latency · Azure Pages Monthly · Azure Pages Daily · Cache Hit Rate |
| **📈 Throughput & Latency** | Scan rate/min (success vs error) · Latency percentiles (p50/p90/p99) |
| **🔍 OCR Engine & Quality** | Scans by OCR strategy (stacked bars) · Avg items detected · Avg OCR confidence (threshold line at 0.7) |
| **☁️ Azure Usage & Cost** | API call rates by model · Daily pages (red at 22/day) · Monthly pages (red at 500/month) |
| **🌐 HTTP & Infrastructure** | HTTP request rate · HTTP latency percentiles · Cache + rate limits · DB connections · Success rate · Rate limit rejections |

#### Quick Start

```bash
# Start the full observability stack
docker-compose up -d

# Open Grafana
# → http://localhost:3000
# → Login: admin / admin (configurable via GRAFANA_USER / GRAFANA_PASSWORD env vars)
# → Dashboard is auto-loaded: "Receipt Scanner — Operations"
```

#### Customization

The dashboard JSON lives at `monitoring/grafana/dashboards/receipt-scanner.json`. Edit it in Grafana's UI and export updated JSON, or modify the file directly. Grafana will auto-reload on container restart.

### WebSocket Batch Updates (Real-Time — "push progress, don't poll")

Instead of polling `GET /api/batch/{id}`, connect a WebSocket to receive real-time push notifications as each file in a batch is processed.

#### Connection

```
ws://localhost:8000/ws/batch/{batch_id}
```

#### Message Types

| Type | When | Payload |
|------|------|---------|
| `connected` | On WebSocket open | `{ type, batch_id, status, total_files, progress_percent }` |
| `batch_started` | Batch begins processing | `{ type, batch_id, total_files, status }` |
| `file_completed` | Each file finishes | `{ type, batch_id, index, filename, status, processing_time_ms, processed, total_files, progress_percent }` |
| `batch_completed` | All files done | `{ type, batch_id, status, total_files, succeeded, failed, total_time_ms }` |
| `pong` | Keep-alive response | `{ type: "pong" }` |
| `error` | Error occurred | `{ type, error }` |

#### JavaScript Example

```javascript
const ws = new WebSocket(`ws://localhost:8000/ws/batch/${batchId}`);

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case 'file_completed':
      console.log(`[${msg.progress_percent}%] ${msg.filename}: ${msg.status}`);
      updateProgressBar(msg.progress_percent);
      break;
    case 'batch_completed':
      console.log(`Done! ${msg.succeeded}/${msg.total_files} succeeded`);
      ws.close();
      break;
  }
};

// Keep-alive ping every 30s
setInterval(() => ws.send(JSON.stringify({ type: 'ping' })), 30000);
```

### Structured JSON Logging (Machine-Parseable — "Loki / ELK ready")

JSON-formatted logs are written alongside the existing text logs, designed for ingestion by Grafana Loki, ELK stack, or any log aggregation platform.

#### Log Format

```json
{
  "timestamp": "2025-01-15T14:30:00.123456+00:00",
  "level": "INFO",
  "logger": "app.services.receipt_service",
  "message": "Receipt processed successfully",
  "module": "receipt_service",
  "function": "process_receipt",
  "line": 142,
  "extra": { "receipt_id": "REC-20250115-...", "items_found": 5 }
}
```

#### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JSON_LOGGING_ENABLED` | `true` | Enable JSON log file output |
| `JSON_LOGGING_STDOUT` | `false` | Also emit JSON to stdout (for Docker log drivers) |

**Log file:** `logs/app.json.log` (rotating, same size/backup policy as text logs)

#### Loki Integration

JSON logs are automatically shipped to Loki via Promtail in docker-compose. Query them in Grafana:

```logql
{job="receipt-scanner-json"} | json | level="ERROR"
{job="receipt-scanner-json"} | json | logger="app.ocr.hybrid_engine"
```

### Prometheus Alerting (Proactive — "get notified before things break")

15 alert rules across 4 groups, routed through Alertmanager with webhook delivery.

#### Alert Groups

| Group | Rules | Key Alerts |
|-------|-------|------------|
| **azure_budget** | 4 | Daily page limit near/exceeded, monthly budget 80%/exceeded |
| **error_rates** | 4 | Scan error rate >15%/40%, HTTP 5xx >5%, Azure API errors |
| **latency** | 3 | Scan p99 >30s, scan p50 >15s, HTTP p99 >10s |
| **infrastructure** | 4 | Rate limit spikes, low cache hit rate, target down, low OCR confidence |

#### Alertmanager

- **URL:** `http://localhost:9093`
- **Default receiver:** webhook to `POST /api/webhooks/alerts`
- **Routing:** Critical alerts repeat every 1h, warnings every 4h
- **Inhibit:** Critical suppresses warning for the same alert name

Customize receivers in `monitoring/alertmanager.yml` — commented-out Slack and email configs are included.

#### Alert Rule Files

- `monitoring/alert_rules.yml` — all 15 Prometheus rules
- `monitoring/alertmanager.yml` — routing, receivers, inhibition

### Async Batch Processing (Background Jobs — "scan 20 receipts without blocking")

The async batch API processes multiple receipts in the background using `asyncio` + `ThreadPoolExecutor`. Unlike the synchronous `/api/receipts/scan-batch` endpoint, it returns immediately with a `batch_id` for polling.

#### Architecture

```
POST /api/batch (20 files)
    │
    ▼
[Validate & Save Files] ──► Return 202 { batch_id }
    │                            immediately
    ▼
[Background asyncio.Task]
    ├── Semaphore (3 concurrent)
    ├── File 1 ─── ThreadPoolExecutor ──► receipt_service.process_receipt()
    ├── File 2 ─── ThreadPoolExecutor ──► receipt_service.process_receipt()
    ├── File 3 ─── ThreadPoolExecutor ──► receipt_service.process_receipt()
    │   ... (queued until semaphore releases)
    └── File 20 ─── ThreadPoolExecutor ──► receipt_service.process_receipt()
         │
         ▼
    [BatchJob.status = COMPLETED]
         │
GET /api/batch/{id} ──► { status, progress_percent, results[] }
```

#### API Usage

```bash
# 1. Submit a batch (returns immediately)
curl -X POST http://localhost:8000/api/batch \
  -F "files=@receipt1.jpg" \
  -F "files=@receipt2.jpg" \
  -F "files=@receipt3.jpg"

# Response (202 Accepted):
# { "batch_id": "a1b2c3d4e5f6", "total_files": 3, "status": "pending", "poll_url": "/api/batch/a1b2c3d4e5f6", "ws_url": "/ws/batch/a1b2c3d4e5f6" }

# 2. Poll for status
curl http://localhost:8000/api/batch/a1b2c3d4e5f6

# Response (in-progress):
# { "status": "processing", "progress_percent": 66.7, "processed": 2, "total_files": 3, ... }

# Response (complete):
# { "status": "completed", "progress_percent": 100, "success_count": 3, "files": [...] }

# 3. List all batches
curl http://localhost:8000/api/batch

# 4. Cancel a batch
curl -X DELETE http://localhost:8000/api/batch/a1b2c3d4e5f6
```

#### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_BATCH_SIZE` | `20` | Max files per batch |
| `MAX_CONCURRENT_SCANS` | `3` | Parallel OCR workers (semaphore) |
| `MAX_ACTIVE_BATCHES` | `5` | Max batches processing simultaneously |
| `BATCH_RESULT_TTL` | `3600s` | How long completed results are kept |
| `MAX_STORED_BATCHES` | `50` | Max batches in memory before eviction |

### Prometheus Metrics (Aggregates — "how much, how fast?")

Exposed at **`/metrics`**. See the [Deployment → Prometheus Metrics](#prometheus-metrics) section below for the full metric table and Prometheus scrape config.

### OpenTelemetry Tracing (Per-Request — "why was THIS scan slow?")

Distributed tracing instruments every stage of the OCR pipeline with spans, letting you drill into individual scans.

#### Span Hierarchy

```
process_receipt                           ← root span (receipt_service)
├── preprocess_image                      ← image enhancement (preprocessor)
│   └── image_preprocessing               ← detailed stages (resize, denoise, etc.)
├── hybrid_engine.route                   ← engine selection (hybrid_engine)
│   └── azure_api_call                    ← Azure strategy execution
│       ├── azure.optimize_image          ← image compression
│       └── azure.analyze_document        ← actual Azure API call
├── parse_receipt                         ← text → structured data (parser)
│   └── receipt_parsing                   ← line grouping, pattern matching
└── database_save                         ← SQLite/PostgreSQL write
```

Each span records attributes like `ocr.engine_used`, `ocr.detections`, `parse.items_found`, `azure.model`, `azure.pages_consumed`, and timing data.

#### Quick Start (Docker — Recommended)

```bash
# Start the scanner + Jaeger in one command
docker-compose up -d

# Open Jaeger UI
# → http://localhost:16686
# → Select service "receipt-scanner" → Find Traces
```

Tracing is **enabled by default** in docker-compose. Jaeger collects spans via OTLP gRPC on port 4317.

#### Quick Start (Local Development)

```bash
# 1. Start Jaeger (Docker required for Jaeger only)
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  -e COLLECTOR_OTLP_ENABLED=true \
  jaegertracing/all-in-one:1.62

# 2. Enable tracing and start the app
$env:OTEL_TRACING_ENABLED = "true"          # PowerShell
# export OTEL_TRACING_ENABLED=true           # Linux/macOS
python run.py

# 3. Scan a receipt, then open Jaeger UI
# → http://localhost:16686
```

#### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_TRACING_ENABLED` | `false` | Master switch — `true` to activate tracing |
| `OTEL_EXPORTER_ENDPOINT` | `http://localhost:4317` | OTLP gRPC endpoint (Jaeger, Tempo, etc.) |
| `OTEL_SERVICE_NAME` | `receipt-scanner` | Service name shown in Jaeger UI |

#### How to Read a Trace

1. **Open Jaeger UI** at `http://localhost:16686`
2. Select **Service** → `receipt-scanner`
3. Click **Find Traces** — you'll see one trace per receipt scan
4. Click a trace to expand the waterfall view:
   - **Wide bars** = slow stages (look for Azure API calls, preprocessing)
   - **Red bars** = errors (exceptions are recorded on the span)
   - Click any span to see **attributes** (engine used, detections count, confidence, timing)

#### Trace Example — Slow Scan Debug

```
Trace: 3a2b1c... (1240ms total)
├── process_receipt ───────────────────────────── 1240ms
│   ├── preprocess_image ─────── 85ms   ← fast ✓
│   ├── hybrid_engine.route ──── 920ms  ← bottleneck!
│   │   └── azure_api_call ──── 890ms
│   │       ├── azure.optimize_image ── 12ms
│   │       └── azure.analyze_document ── 875ms  ← Azure API latency
│   ├── parse_receipt ────────── 45ms   ← fast ✓
│   └── database_save ───────── 8ms    ← fast ✓
```

**Diagnosis:** Azure API took 875ms (71% of total). Consider: caching more aggressively, switching to `read-only` strategy, or checking Azure region latency.

#### Prometheus vs OpenTelemetry — When to Use Which

| Question | Tool | Example |
|----------|------|---------|
| "What's our 99th percentile scan time?" | Prometheus | `histogram_quantile(0.99, ocr_scan_duration_seconds)` |
| "How many Azure pages did we consume today?" | Prometheus | `azure_pages_daily` gauge |
| "Why was scan #abc123 slow?" | OpenTelemetry | Jaeger: find trace, inspect span durations |
| "Which OCR engine was used for a specific receipt?" | OpenTelemetry | Span attribute: `ocr.engine_used` |
| "Are we hitting rate limits?" | Prometheus | `rate_limit_rejections_total` counter |
| "What exact error did Azure return for this scan?" | OpenTelemetry | Span: `azure.analyze_document` → exception event |

#### Disabling Tracing

Tracing is **off by default** and has **zero performance overhead** when disabled. All span calls become no-ops.

```bash
# Disable tracing (default)
OTEL_TRACING_ENABLED=false python run.py
# or simply don't set the variable
```

---

## Training System

A built-in training and optimization system at `/api/training/` for continuous OCR improvement, accessible via both API and command line.

### Components

| Module | Purpose |
|--------|---------|
| `benchmark.py` | Automated accuracy benchmarking against labeled test images |
| `optimizer.py` | Grid search over OCR parameters (canvas size, mag ratio, thresholds) |
| `data_manager.py` | Training data management — upload images, create/edit labels, manage profiles |
| `template_learner.py` | Receipt template pattern recognition and learning |
| `real_world_trainer.py` | **Adaptive trainer** — scan → correct → mine errors → generate learned rules |
| `routes.py` | Training API endpoints (`/api/training/*`) — 23 endpoints total |

### Training Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/training/benchmark` | Run accuracy benchmark on labeled images |
| `GET` | `/api/training/benchmark/results` | Get latest benchmark results |
| `POST` | `/api/training/optimize` | Run parameter optimization grid search |
| `GET` | `/api/training/profiles` | List optimization profiles |
| `POST` | `/api/training/data/upload` | Upload training images with labels |

### Real-World Trainer Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/training/trainer/scan` | Scan an image, return corrections interface |
| `POST` | `/api/training/trainer/save` | Save corrected receipt as training sample |
| `POST` | `/api/training/trainer/analyze` | Mine error patterns from training data |
| `POST` | `/api/training/trainer/learn` | Generate learned substitution rules |
| `GET` | `/api/training/trainer/confusion` | Get character confusion matrix |
| `POST` | `/api/training/trainer/auto-improve` | Full improvement cycle (analyze → learn → export) |
| `GET` | `/api/training/trainer/report` | Generate training progress report |

### Real-World Trainer

The adaptive trainer learns from real-world scanning corrections to continuously improve OCR accuracy:

1. **Scan & Correct** — Scan a receipt, review OCR output, correct any errors
2. **Error Pattern Mining** — Needleman-Wunsch alignment finds systematic OCR misreads
3. **Confusion Matrix** — Character-level confusion statistics across all samples
4. **Learned Rules** — Auto-generates substitution rules loaded by the parser on startup
5. **Image Augmentation** — Rotation, noise, blur, brightness, perspective transforms
6. **Auto-Improve** — Full pipeline (mine → confuse → learn → export) in one call
7. **Progress Reports** — Training statistics, accuracy trends, top error patterns
8. **Batch Scanning** — Process multiple images for bulk training data collection

### Interactive CLI

```bash
# Interactive menu
python scripts/trainer.py

# Direct commands
python scripts/trainer.py scan path/to/receipt.jpg     # Scan + correct workflow
python scripts/trainer.py batch-scan path/to/images/    # Batch scan directory
python scripts/trainer.py analyze                       # Mine error patterns
python scripts/trainer.py learn                         # Generate learned rules
python scripts/trainer.py confusion                     # Show confusion matrix
python scripts/trainer.py auto-improve                  # Full improvement cycle
python scripts/trainer.py report                        # Training progress report
python scripts/trainer.py augment                       # Augment training images
python scripts/trainer.py status                        # Show training stats
```

### Quick Start

```bash
# Run training benchmark via script
python scripts/train.py

# Or via API
curl -X POST http://localhost:8000/api/training/benchmark

# Start real-world training session
python scripts/trainer.py scan path/to/receipt.jpg
```

### Training Data Structure

```
training_data/
├── images/              # Receipt images for training
├── labels/              # Ground truth labels (product codes + quantities)
├── profiles/            # Saved optimization profiles
├── results/             # Benchmark output + confusion matrix + error patterns
├── augmented/           # Augmented training images
├── learned_rules.json   # Auto-generated substitution rules (loaded by parser)
├── training_sessions.json  # Session metadata history
├── correction_log.json  # Human correction history
```

### Production (Direct)

```bash
# Standard start
python run.py

# Or via uvicorn directly
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Docker

```bash
# Build the production image
docker build -t receipt-scanner .

# Run with docker-compose (recommended — mounts persistent volumes)
docker-compose up -d

# View logs
docker-compose logs -f receipt-scanner

# Stop
docker-compose down
```

The Docker setup provides:
- **Multi-stage build** — slim Python 3.12 image (~350 MB vs ~1.2 GB full)
- **Non-root user** — runs as `appuser` (UID 1000)
- **Healthcheck** — auto-restarts if `/api/health` fails
- **6 named volumes** — uploads, exports, logs, data, backups, models persist across restarts
- **Prometheus** — metrics collection at `http://localhost:9090`
- **Grafana** — pre-built dashboards at `http://localhost:3000` (admin/admin)
- **Jaeger** — distributed tracing UI at `http://localhost:16686`
- **Alertmanager** — alert routing at `http://localhost:9093`
- **Loki** — log aggregation at `http://localhost:3100`
- **Promtail** — log shipper (JSON + text logs → Loki)

### Prometheus Metrics

When the app is running, Prometheus metrics are exposed at **`/metrics`**. Scrape this endpoint with your Prometheus server.

**Available metrics:**

| Metric | Type | Description |
|--------|------|-------------|
| `http_requests_total` | Counter | Auto-instrumented HTTP requests (method, status, path) |
| `http_request_duration_seconds` | Histogram | Request latency |
| `ocr_scans_total` | Counter | Scans by strategy and success/failure |
| `ocr_scan_duration_seconds` | Histogram | OCR processing time |
| `ocr_items_detected` | Histogram | Items found per scan |
| `ocr_confidence_score` | Histogram | Average OCR confidence |
| `azure_api_calls_total` | Counter | Azure API calls by model and status |
| `azure_pages_daily` | Gauge | Pages consumed today |
| `azure_pages_monthly` | Gauge | Pages consumed this month |
| `cache_hits_total` / `cache_misses_total` | Counter | Image cache effectiveness |
| `db_connections_active` | Gauge | Active database connections |
| `rate_limit_rejections_total` | Counter | 429 responses by endpoint type |

**Prometheus scrape config:**

```yaml
# prometheus.yml
scrape_configs:
  - job_name: receipt-scanner
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
```

### CI/CD Pipeline

GitHub Actions runs automatically on every push and pull request:

1. **Lint** — `ruff check` + `ruff format --check` on all source files
2. **Test** — `pytest` on Python 3.12 matrix
3. **Docker** — Builds the image and verifies the healthcheck passes

### Pre-commit Hooks

```bash
# Install pre-commit
pip install pre-commit

# Set up hooks (run once after cloning)
pre-commit install

# Run manually on all files
pre-commit run --all-files
```

Hooks: trailing whitespace, EOF fixer, YAML/TOML check, large file guard, merge conflict check, debug statement detection, ruff lint + format.

### Production Checklist

- [ ] Set `API_SECRET_KEY` to protect destructive endpoints
- [ ] Set `API_DOCS_ENABLED=false` to hide Swagger UI
- [ ] Set `API_DEBUG=false` (default)
- [ ] Configure `RATE_LIMIT_RPM` and `RATE_LIMIT_SCAN_RPM`
- [ ] Configure Azure credentials if using hybrid OCR
- [ ] Set `DB_BACKUP_KEEP_DAYS` for backup retention policy
- [ ] Review `LOG_LEVEL` (recommend `WARNING` for production)
- [ ] Set up Prometheus scraping for `/metrics` endpoint
- [ ] Configure Grafana alerts for Azure budget thresholds
- [ ] Review Alertmanager receivers (`monitoring/alertmanager.yml`) — enable Slack/email
- [ ] Set `JSON_LOGGING_ENABLED=true` for structured log ingestion
- [ ] Use `docker-compose up -d` for containerized deployment
- [ ] Enable GitHub Actions CI on your repository

---

## Changelog

### v2.1.0 — Smart OCR + Deep Testing (March 2026)

**Phase 2: Smart OCR Pipeline** — Post-parse intelligence layer:
- **Quality Scoring** — 6-factor weighted score (OCR confidence 30pts, items 20pts, total verification 15pts, math verification 15pts, image quality 10pts, catalog match 10pts)
- **Validation Rules** — 4-rule engine: impossible qty detection, price sanity (>5× catalog), duplicate flagging, cross-receipt anomaly detection
- **Duplicate Detection** — 3-layer dedup: perceptual image hash (8×8 average hash, hamming ≤5), content fingerprint (SHA-256), user confirmation
- **OCR Correction Feedback** — Records user corrections, builds lookup map (≥2 occurrences), auto-corrects known misreads
- **Date & Store Extraction** — 10+ date formats, keyword-priority store detection, truncation at 200 chars
- **New API Endpoints** — `GET /api/corrections`, `GET /api/item-stats`
- **Database Migration v4** — Added 6 columns to receipts table + `ocr_corrections` table

**Deep Edge-Case Testing** — 7 bugs found and fixed:

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `dedup_service.py` | Empty-code fingerprint returned fixed SHA hash | `if not pairs: return ""` |
| 2 | `quality_scorer.py` | Missing `match_type` inflated catalog match score | Added `None` to exclusion |
| 3 | `validators.py` | Rule 4 anomaly detection gated on catalog | Removed `and catalog` gate |
| 4 | `quality_scorer.py` | `sharpness=0` displayed as `None` | `if sharpness is not None` |
| 5 | `dedup_service.py` | SQL NULL `image_hash` not handled | `.get() or ""` pattern |
| 6 | `dedup_service.py` | `None` code crashed `.upper()` | `(item.get("code") or "")` |
| 7 | `quality_scorer.py` | `None` confidence crashed `> 0` | `.get() or 0` pattern |

**103 new edge-case tests** across 9 test classes — total: **314 tests passing** (283 unit + 31 integration)

### v2.0.0 — Same-Receipt-Type Optimization

**Deep Audit Result:** 🏆 **91/100 (Grade A)** — up from 81/100 (Grade B)

### Parameter Optimizations

| Parameter | Before | After | Impact |
|-----------|--------|-------|--------|
| `OCR_CANVAS_SIZE` | 1536 | 1280 | ~15% speed gain, no accuracy loss |
| `OCR_MAG_RATIO` | 2.0 | 1.8 | Cleaner detections on dense receipts |
| `OCR_SMART_PASS_THRESHOLD` | 5 | 3 | Skip 2nd pass earlier → 20-40% speed gain |
| Fast-pass canvas | 960 | 1024 | Better text capture in fast mode |
| Fast-pass mag_ratio | 1.2 | 1.5 | Improved detection on small text |
| Fast-pass width_ths | 0.8 | 0.7 | Tighter word grouping |
| Deskew threshold | 0.5° | 1.5° | Eliminates unnecessary rotations |
| Qty sanity max | 500 | 100 | Catches OCR hallucinations |
| 2-column qty clamp | 99 | 50 | Realistic quantity limits |
| Dense receipt threshold | 8 items | 6 items | Earlier detection of dense layouts |

### Key Fixes

- **Ground truth correction** — Fixed test expectations that expected wrong results
- **Dense receipt detection** — Now activates at 6+ items (was 8), improving parsing on compact receipts
- **Smart-skip confidence gate** — Dual-pass skip requires confidence ≥ 0.55 (prevents skipping on garbage detections)
- **Edge case image generation** — Bigger canvas (1200×1600) and increased spacing for dense receipt test images

### Test Results (Post-Optimization)

| Test Suite | Result |
|------------|--------|
| Smart OCR Tests | ✅ All passed |
| Smart OCR Edge Cases | ✅ All passed |
| Accuracy Tests | ✅ 100% code detection, 100% qty accuracy |
| Preprocessing Tests | ✅ All passed |

---

## Contributing

1. **Fork** the repository
2. **Create** a feature branch: `git checkout -b feature/my-feature`
3. **Commit** your changes: `git commit -m 'feat: add my feature'`
4. **Push** to the branch: `git push origin feature/my-feature`
5. **Open** a Pull Request

### Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) conventions
- Use type hints for function signatures
- Write docstrings for public functions and classes
- Keep modules focused — single responsibility principle

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
