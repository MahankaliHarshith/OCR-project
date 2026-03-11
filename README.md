# 📝 Handwritten Receipt Scanner

> An intelligent OCR-powered web application that scans handwritten shop receipts, extracts product codes and quantities, and generates structured Excel reports — built for small retail shops.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.132-009688?logo=fastapi&logoColor=white)
![EasyOCR](https://img.shields.io/badge/EasyOCR-1.7-FF6F00)
![Azure](https://img.shields.io/badge/Azure_Doc_Intel-Optional-0078D4?logo=microsoftazure&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [OCR Pipeline](#ocr-pipeline)
- [Database](#database)
- [Testing](#testing)
- [Deployment](#deployment)
- [Contributing](#contributing)
- [License](#license)

---

## Features

### Core Capabilities

- **Hybrid OCR Engine** — Local EasyOCR + optional Azure Document Intelligence with intelligent cost-aware routing
- **Handwritten Receipt Parsing** — 7 regex patterns with 4-tier fuzzy code matching (exact → OCR-sub → handwriting-sub → fuzzy)
- **Real-time Web Interface** — Single-page app with camera capture, clipboard paste, drag-and-drop upload
- **Excel Export** — Styled multi-sheet reports (Daily Sales + Summary) with confidence highlighting
- **Product Catalog** — Full CRUD with CSV import/export and fuzzy search

### Accuracy

- **100% code detection** across 25+ test receipts (original, edge-case, and real-world images)
- **96–100% quantity accuracy** — limited only by inherent OCR ambiguity on heavily inked receipts
- **Cross-line total verification** with OCR-garbled variant handling

### Production-Ready

- **6-Layer Azure Cost Defense** — image quality gate, local-first skip, daily/monthly limits, budget pacing, image cache, model strategy selection
- **Security Hardening** — CSP headers, rate limiting (10 scan / 30 general RPM), API key protection, magic-byte file validation, path traversal guards
- **Database** — SQLite WAL with connection pooling, versioned schema migrations, daily auto-backups. Optional PostgreSQL drop-in swap.
- **Observability** — Rotating file + console logging, per-stage processing logs, dashboard with parallel DB queries

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
│  receipt_service  ·  product_service  ·  excel_service      │
├─────────────────────────────────────────────────────────────┤
│                   Hybrid OCR Engine                         │
│  ┌──────────┐    ┌──────────────┐    ┌─────────────────┐   │
│  │ EasyOCR  │◄──►│ Hybrid Router│◄──►│ Azure Doc Intel │   │
│  │ (local)  │    │ (cost-aware) │    │   (optional)    │   │
│  └──────────┘    └──────────────┘    └─────────────────┘   │
│  preprocessor  ·  parser  ·  image_cache  ·  usage_tracker  │
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

## Project Structure

```
├── run.py                        # Application entry point
├── requirements.txt              # Pinned Python dependencies
├── .env.example                  # Environment variable template
├── .gitignore
│
├── app/                          # Application source code
│   ├── __init__.py               #   Package metadata (version, author)
│   ├── main.py                   #   FastAPI app factory + lifespan
│   ├── config.py                 #   Centralized configuration (env vars)
│   ├── middleware.py             #   Security, rate limiting, CORS, caching
│   ├── database.py               #   SQLite backend, pool, migrations, backups
│   ├── db_postgres.py            #   PostgreSQL backend (drop-in swap)
│   ├── logging_config.py         #   Rotating file + console log setup
│   │
│   ├── api/
│   │   └── routes.py             #   REST endpoint definitions
│   │
│   ├── ocr/
│   │   ├── engine.py             #   EasyOCR wrapper (3 speed tiers)
│   │   ├── azure_engine.py       #   Azure Document Intelligence client
│   │   ├── hybrid_engine.py      #   Intelligent OCR router (cost-aware)
│   │   ├── preprocessor.py       #   Image enhancement pipeline
│   │   ├── parser.py             #   Receipt text → structured data
│   │   ├── total_verifier.py     #   Cross-line total verification
│   │   ├── usage_tracker.py      #   Azure API budget tracking
│   │   └── image_cache.py        #   SHA-256 LRU result cache
│   │
│   ├── services/
│   │   ├── receipt_service.py    #   Scan orchestration pipeline
│   │   ├── product_service.py    #   Product CRUD + CSV import/export
│   │   └── excel_service.py      #   Styled Excel report generation
│   │
│   └── static/                   # Frontend assets
│       ├── index.html            #   3-tab SPA (Scan | Receipts | Catalog)
│       ├── styles.css            #   Glassmorphic UI with CSS variables
│       ├── app.js                #   Client-side logic + camera/clipboard
│       └── lucide.min.js         #   Icon library
│
├── tests/                        # Test suite
│   ├── test_app.py               #   Unit tests (pytest) — parser, Excel, DB
│   ├── test_db_production.py     #   Database infrastructure tests (46 tests)
│   ├── test_codes.py             #   Fuzzy matching tests
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
│   ├── dev/                      #   Development & debugging tools
│   └── generators/               #   Test data generation scripts
│
├── docs/                         # Documentation
│   ├── PRD.txt                   #   Product Requirements Document
│   ├── CONTEXT.md                #   Technical context reference
│   ├── HYBRID_OCR_ARCHITECTURE.md
│   ├── DEEP_AUDIT_REPORT.md
│   └── Receipt_Design_and_Scanning_Guide.md
│
├── models/                       # EasyOCR model weights (auto-downloaded)
├── uploads/                      # Uploaded receipt images (auto-cleaned 7d)
├── exports/                      # Generated Excel/CSV files (auto-cleaned 7d)
├── logs/                         # Application log files (rotating)
├── data/                         # Runtime data (usage stats, image cache)
└── backups/                      # Daily SQLite snapshots (auto-pruned 7d)
```

---

## Getting Started

### Prerequisites

- **Python 3.11+**
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
| `LOCAL_CONFIDENCE_SKIP_THRESHOLD` | `0.72` | Skip Azure if local OCR confidence ≥ this |
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
[Local EasyOCR] ── conf ≥ 0.72 ──► Return local result
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

### Image Preprocessing Pipeline

1. **Load** — EXIF-corrected orientation → resize to max 1800px
2. **Grayscale** → deskew via Hough line transform (±15°)
3. **Quality assessment** — Laplacian sharpness + mean brightness
4. **Enhancement** — Gaussian blur, unsharp mask, bilateral filter (adaptive)
5. **Morphology** — conditional closing + CLAHE + brightness normalization
6. **Crop** — Otsu threshold → bounding box with 5% margin

### Receipt Parsing

- **7 regex patterns** for code–quantity extraction (priority-ordered)
- **4-tier code matching:** exact → OCR character substitution → handwriting substitution → fuzzy (difflib)
- **Y-aware line grouping** with rotation-resistant quantity alignment
- **Cross-line total verification** with OCR-garbled variant handling (`qtyt`, `grrand`, etc.)

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
| `products` | Product catalog — code (unique), name, category, unit |
| `receipts` | Scan metadata — image paths, status, OCR confidence |
| `receipt_items` | Parsed line items (FK → receipts, CASCADE delete) |
| `processing_logs` | Per-stage timing and error tracking |
| `schema_migrations` | Migration version audit trail |

### Seed Data

On first initialization, the database is seeded with 10 paint-shop products:
`ABC`, `XYZ`, `PQR`, `MNO`, `DEF`, `GHI`, `JKL`, `STU`, `VWX`, `RST`.

---

## Testing

### Unit Tests

```bash
# Run core unit tests (parser, Excel, DB)
python -m pytest tests/test_app.py -v

# Database infrastructure tests (connection pool, migrations, backups)
python tests/test_db_production.py
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

## Deployment

### Development

```bash
# Hot reload + debug logging
API_DEBUG=true LOG_LEVEL=DEBUG python run.py
```

### Production

```bash
# Standard start
python run.py

# Or via uvicorn directly
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Production Checklist

- [ ] Set `API_SECRET_KEY` to protect destructive endpoints
- [ ] Set `API_DOCS_ENABLED=false` to hide Swagger UI
- [ ] Set `API_DEBUG=false` (default)
- [ ] Configure `RATE_LIMIT_RPM` and `RATE_LIMIT_SCAN_RPM`
- [ ] Configure Azure credentials if using hybrid OCR
- [ ] Set `DB_BACKUP_KEEP_DAYS` for backup retention policy
- [ ] Review `LOG_LEVEL` (recommend `WARNING` for production)

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
