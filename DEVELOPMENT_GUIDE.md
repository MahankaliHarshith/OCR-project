# ⛔ AGENT INSTRUCTION FILE — READ BEFORE EVERY CODE CHANGE

> **This file is the law.** Every line of code you write for this project must
> comply with the rules below. Violations cause real bugs that reach the user.
> This is NOT generic advice — every rule maps to a bug that was shipped and
> had to be fixed in production.

---

## 🔴 MANDATORY PRE-FLIGHT CHECKLIST

**Before writing ANY code, answer these three questions:**

1. **Does the function I'm touching get called more than once?**
   - If YES → you MUST use event delegation or guard against duplicate listeners.
   - Functions that re-render: `populateItemsTable`, `renderEditableTable`,
     `loadReceipts`, `rerenderReceiptCards`, `loadCatalog`, `_renderCatalogPage`.
   - These are called on every tab switch, every data change, every receipt load.
   - NEVER attach `addEventListener` inside these functions without delegation.

2. **Does any string I generate involve dates, XML, or file paths?**
   - Dates → MUST use `str(dt.day)` or `f"{dt.day}"` in Python. NEVER `strftime("%-d")`.
   - XML → NEVER manually prepend `<?xml?>` if using `toprettyxml()` or `toxml()`.
   - Paths → MUST use `pathlib.Path` / `os.path.join`. NEVER hardcode `/` or `\\`.

3. **Am I restoring state after a UI operation completes?**
   - Tab switch → old tab's data fetches MUST be aborted (AbortController).
   - Catalog search clear → MUST restore full list from cache, not re-fetch.
   - Modal close → MUST clean up overlay click listener.
   - Timer / interval → MUST be cleared in error paths AND success paths.

---

## 📐 PROJECT ARCHITECTURE

### Tech Stack
| Layer | Technology | Notes |
|-------|-----------|-------|
| Backend | FastAPI + Uvicorn | Python 3.12, auto-reload in dev |
| Frontend | Vanilla JS (single file) | ~5,700 lines in `app/static/app.js` |
| Styles | Single CSS file | ~4,700 lines in `app/static/styles.css` |
| HTML | Single HTML file | ~1,100 lines in `app/static/index.html` |
| Database | SQLite (local) | `%LOCALAPPDATA%\ReceiptScanner\receipt_scanner.db` |
| OCR | EasyOCR (local) + Azure (cloud) | Hybrid dual-pass architecture |
| OS Target | **Windows** | All code MUST work on Windows first |

### Frontend State Object (app.js line ~30)
```javascript
const state = {
    currentTab: 'scan',           // 'scan' | 'receipts' | 'products' | 'train'
    batches: _loadBatches(),      // Array of { id, name, receiptIds, created }
    activeBatchId: ...,           // string | null
    currentReceiptData: null,     // Current receipt being edited
    editingProduct: null,         // Product being edited in catalog
    catalogCache: {},             // code → { name, unit, category }
    isProcessing: false,          // Prevents double-uploads
    progressInterval: null,       // Progress bar simulation timer
    isDirty: false,               // Unsaved edits flag
    confirmed: false,             // Receipt confirmed flag
    selectedReceiptIds: new Set(),// Bulk selection (Receipts tab)
    _allLoadedReceipts: [],       // Cached receipt list
    _removedItemIds: [],          // Soft-deleted item IDs (undo support)
    _abortController: null,       // OCR scan abort controller
};
```

### Tab System
- Tabs: `scan`, `receipts`, `products`, `train`
- Switching tabs creates a new `AbortController` and aborts the old one
- Each tab has a load function: `loadReceipts()`, `loadCatalog()`, `loadTrainingTab()`
- The `_tabAbortController` variable tracks the active controller

### Hidden State Variables (NOT in `state` object)
```
_catalogFullProductList  — full product list for search restore
_catalogSortKey          — current catalog sort column
_catalogSortAsc          — sort direction flag
_catalogSearchQuery      — current search text
_catalogPage             — current pagination page
_productNameDebouncers   — Map for auto-fill debounce timers
_activeOverlayClose      — modal overlay close handler reference
```

### localStorage Keys
| Key | Purpose |
|-----|---------|
| `scannerBatches` | JSON array of batch objects (persisted batch state) |
| `batchReceiptIds` | Legacy — old format, auto-migrated on load |
| `activeBatchId` | ID of the currently active batch |
| `trainOnboardingDismissed` | Boolean — training onboarding dismissed |
| `theme` | `'dark'` or `'light'` theme preference |

### Critical Re-Render Functions
These functions rebuild DOM and are called repeatedly. **NEVER add listeners inside them:**

| Function | Line | Called When | Danger |
|----------|------|------------|--------|
| `populateItemsTable(items)` | ~1401 | Receipt loaded/edited | Rebuilds item rows |
| `renderEditableTable(items)` | (inline) | Items displayed for editing | Per-cell inputs |
| `loadReceipts(limit, signal)` | ~2471 | Receipts tab opened | Rebuilds card list |
| `rerenderReceiptCards()` | ~2391 | After delete/batch change | Re-filters existing |
| `loadCatalog(signal)` | ~3263 | Products tab opened | Fetches + renders |
| `_renderCatalogPage()` | ~3203 | Sort/search/paginate | Rebuilds table body |

### Frontend Utility Functions
| Function | Line | Purpose |
|----------|------|---------|
| `$(sel)` | ~4010 | `document.querySelector(sel)` shorthand |
| `$$(sel)` | ~4014 | `document.querySelectorAll(sel)` shorthand |
| `escHtml(str)` | ~4020 | Escape HTML via textContent (XSS protection) |
| `escAttr(str)` | ~4025 | Escape for HTML attributes (quotes) |
| `safeJson(res)` | ~4030 | Safely parse fetch response, returns `{}` on failure |
| `showToast(msg, type)` | ~3990 | Toast notification — types: `success`, `error`, `warning`, `info` |
| `showDeleteConfirm(title, msg, onConfirm)` | ~2401 | Delete confirmation modal |
| `showActionConfirm(title, msg, label, onConfirm)` | ~2430 | Generic action confirmation modal |
| `showPromptModal(title, msg, placeholder, onSubmit)` | ~2460 | Input prompt modal |
| `closeActiveModal()` | ~2495 | Close the currently active modal |
| `trapFocus(el)` | ~4080 | Accessibility focus trap for modals |
| `toggleTheme()` | ~4100 | Dark/light mode toggle |

---

## 🗄️ DATABASE SCHEMA

### Migration System
Migrations auto-apply on startup. Tracked in `schema_migrations` table.

| Version | Name | Description |
|---------|------|-------------|
| 1 | `baseline_schema` | Creates all base tables |
| 2 | `composite_item_index` | Composite index on receipt_items |
| 3 | `add_price_columns` | Adds unit_price, line_total + seeds prices |
| 4 | `smart_ocr_metadata` | Dedup hashes, quality scoring, corrections table |

### Tables

#### `products`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| product_code | VARCHAR(10) UNIQUE | NOT NULL |
| product_name | VARCHAR(200) | NOT NULL |
| category | VARCHAR(50) | |
| unit | VARCHAR(20) | DEFAULT 'Piece' |
| is_active | BOOLEAN | DEFAULT 1 (soft-delete) |
| unit_price | REAL | DEFAULT 0.0 |

#### `receipts`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| receipt_number | VARCHAR(50) UNIQUE | NOT NULL |
| scan_date | DATE | NOT NULL |
| scan_time | TIME | NOT NULL |
| image_path | VARCHAR(500) | |
| processing_status | VARCHAR(20) | DEFAULT 'pending' |
| total_items | INTEGER | DEFAULT 0 |
| ocr_confidence_avg | REAL | |
| bill_total | REAL | DEFAULT 0.0 |
| image_hash | VARCHAR(64) | Dedup (v4) |
| content_fingerprint | VARCHAR(64) | Dedup (v4) |
| receipt_date | DATE | Extracted date (v4) |
| store_name | VARCHAR(200) | Extracted store (v4) |
| quality_score | INTEGER | Image quality 0-100 (v4) |
| quality_grade | VARCHAR(1) | A/B/C/D/F (v4) |

#### `receipt_items`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| receipt_id | INTEGER FK | → receipts(id) ON DELETE CASCADE |
| product_code | VARCHAR(10) | NOT NULL |
| product_name | VARCHAR(200) | |
| quantity | REAL | NOT NULL |
| unit | VARCHAR(20) | |
| ocr_confidence | REAL | |
| manually_edited | BOOLEAN | DEFAULT 0 |
| unit_price | REAL | DEFAULT 0.0 (v3) |
| line_total | REAL | DEFAULT 0.0 (v3) |

#### `ocr_corrections` (v4)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| receipt_id | INTEGER FK | → receipts(id) ON DELETE SET NULL |
| item_id | INTEGER | |
| original_code | VARCHAR(10) | NOT NULL |
| corrected_code | VARCHAR(10) | NOT NULL |
| original_confidence | REAL | |
| new_confidence | REAL | |
| correction_context | TEXT | |

---

## 🌐 API ENDPOINTS (40 total in `app/api/routes.py`)

**System:**
- `GET /api/health` · `GET /api/health/live` · `GET /api/health/ready`
- `GET /api/observability` · `GET /api/dashboard`
- `GET /uploads/{filename}` · `POST /api/webhooks/alerts`

**Receipts & Items:**
- `POST /api/receipts/scan` — single image OCR (multipart upload)
- `POST /api/receipts/scan-batch` — multi-image batch OCR
- `GET /api/receipts` · `GET /api/receipts/{id}` · `DELETE /api/receipts/{id}`
- `GET /api/receipts/date/{date}`
- `PUT /api/receipts/items/{item_id}` · `POST /api/receipts/{id}/items`
- `DELETE /api/receipts/items/{item_id}`

**Batch Processing:**
- `POST /api/batch` · `GET /api/batch` · `GET /api/batch/{id}` · `DELETE /api/batch/{id}`
- `WS /ws/batch/{batch_id}` (WebSocket)

**Products / Catalog:**
- `GET /api/products` · `GET /api/products/search` · `GET /api/products/{code}`
- `POST /api/products` · `PUT /api/products/{code}` · `DELETE /api/products/{code}`
- `GET /api/products/export/csv` · `POST /api/products/import/csv`

**Export:**
- `POST /api/export/excel` · `GET /api/export/daily`
- `GET /api/export/download/{filename}`
- `POST /api/export/tally` · `GET /api/export/tally/daily`

**OCR Engine:**
- `GET /api/ocr/status` · `GET /api/ocr/usage`
- `POST /api/ocr/usage/reset-daily` · `POST /api/ocr/cache/clear`

**Smart OCR:**
- `GET /api/corrections` · `GET /api/item-stats`

### API Response Shapes

**Receipt object:**
```json
{
  "id": 1,
  "receipt_number": "REC-20260319-103000-a1b2c3",
  "scan_date": "2026-03-19", "scan_time": "10:30:00",
  "image_path": "upload_20260319_103000_a1b2c3.jpg",
  "processing_status": "completed",
  "total_items": 3, "ocr_confidence_avg": 0.91,
  "bill_total": 1250.00,
  "items": [
    { "id": 1, "receipt_id": 1, "product_code": "ABC",
      "product_name": "1L Exterior Paint", "quantity": 2.0,
      "unit": "Litre", "ocr_confidence": 0.95,
      "unit_price": 200.0, "line_total": 400.0 }
  ]
}
```

**Products list:**
```json
{ "products": [...], "count": 18, "total": 18 }
```

**Receipts list:**
```json
{ "receipts": [...], "count": 10, "total": 42, "limit": 20, "offset": 0 }
```

**Error response:**
```json
{ "detail": "Unsupported file type '.pdf'. Allowed: .jpg, .jpeg, .png, .bmp" }
```

### Pydantic Models (defined inline in routes.py)
| Model | Key Fields |
|-------|-----------|
| `ProductCreate` | `product_code` (1-10, alphanumeric+upper), `product_name`, `category`, `unit` |
| `ProductUpdate` | All optional (PATCH semantics) |
| `ItemUpdate` | `product_code`, `product_name`, `quantity` (1-99999), `unit`, `unit_price` |
| `ExcelGenerateRequest` | `receipt_ids: list[int]` |
| `TallyExportRequest` | `receipt_ids`, `company_name`, `format` (xml/json), `purchase_ledger`, `cash_ledger` |

**Validation pattern:** `@field_validator` with `.strip().upper()` for codes, regex `^[A-Z0-9_\-]{1,10}$`.

---

## 🔴 ABSOLUTE RULES (1–10)

These rules are non-negotiable. Every one corresponds to a real bug.

### Rule 1: Event Delegation for Re-Rendered Content
**Bug:** Listeners added per-input in `populateItemsTable()` accumulated on every
re-render. After 5 edits, each keystroke fired 5 handlers.

```javascript
// ❌ NEVER — inside a function called more than once
function populateItemsTable(items) {
    items.forEach(item => {
        const input = document.createElement('input');
        input.addEventListener('input', handler);  // LEAK!
    });
}

// ✅ ALWAYS — delegate on the parent, attach ONCE
const tbody = document.querySelector('#itemsTable tbody');
tbody.addEventListener('input', (e) => {
    if (e.target.matches('input[data-field]')) { /* handle */ }
});
```

**How to check:** Search for `addEventListener` inside any function listed in the
"Critical Re-Render Functions" table. If you find one, refactor to delegation.

### Rule 2: One-Time Listeners Need `{ once: true }`
**Bug:** Inline edit keydown handlers stacked on every edit click.

```javascript
// ❌ NEVER
cell.addEventListener('keydown', handler);

// ✅ ALWAYS — for listeners that should fire once then clean up
cell.addEventListener('keydown', handler, { once: true });
```

### Rule 3: Never Register the Same Listener in Two Places
**Bug:** Catalog sort headers had listeners in both `DOMContentLoaded` AND
`loadCatalog()`. Clicking "Name" sorted twice.

**How to check:** Before adding a listener, grep the entire file for the element
selector. If a listener is already registered globally, do NOT add another in a
per-render function.

### Rule 4: Windows Date Formatting
**Bug:** `strftime("%-d")` crashes on Windows with `Invalid format string`.

```python
# ❌ NEVER — Unix-only format codes
date_str = now.strftime("%-d %B %Y")   # crashes on Windows
date_str = now.strftime("%#d %B %Y")   # ALSO unreliable

# ✅ ALWAYS — explicit string conversion
date_str = f"{now.day} {now.strftime('%B %Y')}"
day_no_pad = str(now.day)
```

**Banned format codes on Windows:** `%-d`, `%-m`, `%-H`, `%-M`, `%-S`, `%-j`

### Rule 5: XML Generation
**Bug:** Manual `<?xml?>` prepended + `toprettyxml()` adding its own = double
declaration. Browser refused to parse.

```python
# ❌ NEVER — toprettyxml already adds the declaration
xml_str = '<?xml version="1.0"?>\n' + doc.toprettyxml()

# ✅ ALWAYS — let minidom handle it
xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
```

### Rule 6: UTF-8 Everywhere
```python
# ❌ NEVER
with open(path, "w") as f:

# ✅ ALWAYS
with open(path, "w", encoding="utf-8") as f:
```

### Rule 7: File Paths — Use pathlib
```python
# ❌ NEVER
path = base_dir + "/" + filename

# ✅ ALWAYS
path = Path(base_dir) / filename
```

### Rule 8: Clear Timers in ALL Paths
```javascript
// ❌ NEVER — only clear on success
try { /* ... */ clearInterval(state.progressInterval); }
catch(e) { showToast(e.message, 'error'); }  // interval still running!

// ✅ ALWAYS — clear in finally
try { /* ... */ }
catch(e) { showToast(e.message, 'error'); }
finally { clearInterval(state.progressInterval); state.progressInterval = null; }
```

### Rule 9: Cache State Must Survive Operations
**Bug:** Clearing catalog search re-fetched from API, losing sort order.

```javascript
// ❌ NEVER — re-fetch just to "clear"
searchInput.value = '';
loadCatalog();  // loses sort state

// ✅ ALWAYS — restore from cached full list
searchInput.value = '';
_catalogSearchQuery = '';
_renderCatalogPage();  // uses cached data, preserves sort
```

### Rule 10: AbortController on Tab Switches
```javascript
// ✅ ALWAYS — abort old, create new
let _tabAbortController = null;
function showTab(tab) {
    if (_tabAbortController) _tabAbortController.abort();
    _tabAbortController = new AbortController();
    if (tab === 'receipts') loadReceipts(20, _tabAbortController.signal);
}
```

---

## 🟡 IMPORTANT RULES (11–17)

### Rule 11: Every `fetch()` Needs Error Handling
```javascript
// ✅ ALWAYS
try {
    const res = await fetch('/api/something', { signal });
    if (!res.ok) throw new Error(`Server error: ${res.status}`);
    const data = await res.json();
} catch (e) {
    if (e.name !== 'AbortError') showToast(e.message, 'error');
}
```

### Rule 12: Never Swallow Exceptions
```python
# ❌ NEVER                    # ✅ ALWAYS
except Exception:             except Exception:
    pass                          logger.exception("Failed to process receipt")
```

### Rule 13: Database Operations Need Error Handling
```python
try:
    cursor.execute("INSERT INTO ...", params)
    conn.commit()
except sqlite3.Error as e:
    conn.rollback()
    logger.error(f"DB error: {e}")
    raise
```

### Rule 14: HTML Output Must Be Escaped
```javascript
// ❌ NEVER — XSS               // ✅ ALWAYS
cell.innerHTML = name;           cell.innerHTML = escHtml(name);
```

### Rule 15: Cache Version Bump
When changing JS/CSS/HTML, bump `?v=` in `index.html`:
```html
<script src="app.js?v=2.3.0"></script>
<link href="styles.css?v=2.3.0" rel="stylesheet">
```
Current version: **v2.4.0** — bump to v2.4.1, v2.5.0, etc.

### Rule 16: CSS Animations — GPU Only
```css
/* ❌ NEVER */ .card { transition: margin 0.3s, height 0.3s; }
/* ✅ ALWAYS */ .card { transition: transform 0.3s, opacity 0.3s; }
```

### Rule 17: Z-Index Hierarchy
```
1    — base content       500  — toast notifications
10   — sticky headers     1000 — modals, overlays
100  — dropdowns          1001 — modal content
9999 — critical overlays
```

---

## 🧰 HOW TO: Common Development Tasks

### Add a New API Endpoint

1. **Open** `app/api/routes.py`
2. **Add Pydantic model** (if needed) inline, near the top with other models:
   ```python
   class MyRequest(BaseModel):
       name: str = Field(..., min_length=1, max_length=200)
       @field_validator('name')
       @classmethod
       def sanitize(cls, v: str) -> str:
           return v.strip()
   ```
3. **Add route** using `@router`:
   ```python
   @router.post("/api/my-feature", tags=["MyCategory"])
   async def my_feature(data: MyRequest):
       """Docstring appears in /docs."""
       try:
           result = my_service.do_something(data.name)
           return {"message": "Success", "data": result}
       except ValueError as e:
           raise HTTPException(status_code=400, detail=str(e)) from None
       except Exception as e:
           logger.error(f"my_feature failed: {e}", exc_info=True)
           raise HTTPException(status_code=500, detail="Operation failed.") from None
   ```
4. **Error conventions:**
   - `400` → client validation errors
   - `404` → not found
   - `429` → rate limited
   - `500` → server errors (log real error, return generic message)
   - Always use `from None` to suppress exception chaining

### Add a New Service

1. **Create** `app/services/my_service.py`:
   ```python
   import logging
   from app.config import EXPORT_DIR  # or other config
   logger = logging.getLogger(__name__)

   class MyService:
       """Description."""
       def do_something(self, param: str) -> dict:
           try:
               # ... business logic ...
               return {"result": "value"}
           except Exception:
               logger.exception("Failed in do_something")
               raise

   # Module-level singleton
   my_service = MyService()
   ```
2. **Import in routes:** `from app.services.my_service import my_service`
3. **Key conventions:**
   - Services are **classes** with a **module-level singleton** at the bottom
   - Config comes from `app.config`, NOT hardcoded
   - Every service uses `logger = logging.getLogger(__name__)`
   - Export services write to `EXPORT_DIR` and return file path

### Add a New Frontend Button/Handler

**For static elements (exists in HTML, rendered once):**
```javascript
// At module level — runs once on page load
$('#myBtn').addEventListener('click', async () => {
    const btn = $('#myBtn');
    btn.disabled = true;
    try {
        const res = await fetch('/api/my-feature', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'value' }),
        });
        const data = await safeJson(res);
        if (res.ok) {
            showToast('Success!', 'success');
        } else {
            throw new Error(data.detail || 'Failed');
        }
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
    }
});
```

**For dynamic elements (generated in JS, inside re-rendered content):**
```javascript
// ✅ Use onclick in HTML string or event delegation
// Option A: inline handler (simple actions)
`<button onclick="window._myHandler(${id})">Action</button>`

// window._myHandler defined ONCE at module level:
window._myHandler = async function(id) { /* ... */ };

// Option B: event delegation (complex forms)
parentEl.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action="my-action"]');
    if (btn) { /* handle using btn.dataset.id */ }
});
```

### Add a New Modal Dialog

```javascript
// Use the existing showDeleteConfirm / showActionConfirm / showPromptModal:
showDeleteConfirm('Delete Receipt', 'Are you sure?', async () => {
    await fetch(`/api/receipts/${id}`, { method: 'DELETE' });
    showToast('Deleted', 'success');
});

showActionConfirm('Confirm Export', 'Export 5 receipts?', 'Export', async () => {
    // ... export logic
});

showPromptModal('Rename Batch', 'Enter new name:', 'Batch 1', async (name) => {
    // ... rename logic
});
```

**Key conventions:**
- Single shared `#modalOverlay` — never create new overlay elements
- Previous listeners auto-cleaned by the show* functions
- Confirm buttons use `{ once: true }` to prevent double-fire
- Escape key closes active modal via `_activeModalClose`

### Add a Database Migration

1. **Add migration function** in `app/database.py`:
   ```python
   def _migrate_v5_my_feature(conn):
       """Add my_column to receipts."""
       conn.execute("ALTER TABLE receipts ADD COLUMN my_column TEXT DEFAULT ''")
   ```
2. **Add to MIGRATIONS tuple** at the end:
   ```python
   MIGRATIONS = (
       (1, "baseline_schema", _migrate_v1_baseline),
       (2, "composite_item_index", _migrate_v2_composite_index),
       (3, "add_price_columns", _migrate_v3_price_columns),
       (4, "smart_ocr_metadata", _migrate_v4_smart_metadata),
       (5, "my_feature", _migrate_v5_my_feature),  # ← ADD HERE
   )
   ```
3. **Always use ALTER TABLE** — never DROP and recreate (data loss)
4. **Test:** Delete `receipt_scanner.db` and restart to verify clean creation

### File Upload Flow (for reference)
The OCR scan upload follows this exact sequence:
1. Guard: check `state.isProcessing` → toast if busy
2. Validate file type against allowed extensions
3. Validate file size (≤ 20MB)
4. Client-side quality check (blur/darkness detection)
5. Show processing overlay, start progress simulation interval
6. Compress image client-side
7. `fetch('/api/receipts/scan', { method: 'POST', body: formData, signal })`
8. Parse response → call `populateItemsTable(items)`
9. **Finally:** clear interval, hide overlay, re-enable UI

---

## 🧪 TESTING

### Framework
- **pytest 9.0.2** with `pytest-cov`, `httpx` (for FastAPI TestClient)
- Config in `pyproject.toml`: testpaths=`["tests"]`, addopts=`"-v --tb=short"`
- Coverage minimum: **70%**, source: `["app"]`

### Commands
| Action | Command |
|--------|---------|
| Run all tests | `pytest` |
| With coverage | `pytest --cov=app --cov-report=term-missing` |
| Single file | `pytest tests/test_services.py -v` |
| Single test | `pytest tests/test_api.py::test_health -v` |
| Lint | `ruff check app/ tests/` |
| Format | `ruff format app/ tests/` |
| Type check | `mypy app/` |
| Security scan | `bandit -r app/ -x tests,scripts` |

### Test Patterns Used
```python
# Fixture for test client
@pytest.fixture
def client():
    from app.main import app
    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")

# Mocking database
from unittest.mock import patch, MagicMock
@patch('app.services.receipt_service.db')
def test_something(mock_db):
    mock_db.get_receipt.return_value = {...}

# File upload test
from io import BytesIO
fake_file = BytesIO(b"fake image data")
response = client.post("/api/receipts/scan",
    files={"file": ("test.jpg", fake_file, "image/jpeg")})

# Class-based tests
class TestProductService:
    def setup_method(self):
        # per-test setup
    def test_create(self):
        ...
```

### What to Test When Adding Features
| Change | Test |
|--------|------|
| New API endpoint | Add test in `tests/test_api.py` — happy path + error cases |
| New service method | Add test in `tests/test_services.py` — mock DB, test logic |
| New Pydantic model | Test validation: valid input, invalid input, edge cases |
| DB migration | Test clean creation + migration from previous version |
| Frontend change | Manual test: all 4 tabs, mobile responsive, dark/light mode |

### Pre-Commit Validation
Before declaring ANY change complete, run:
```bash
# 1. Syntax check all modified Python files
python -c "import py_compile; py_compile.compile('app/services/my_service.py', doraise=True)"

# 2. Run related tests
pytest tests/test_services.py -v

# 3. Lint
ruff check app/services/my_service.py

# 4. Start server and verify manually
python run.py
# Then visit http://localhost:8000 and test the feature
```

---

## ⚙️ ENVIRONMENT & SETUP

### Quick Start
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1      # Windows PowerShell
pip install -r requirements.txt
pip install -r requirements-dev.txt  # for testing
python run.py                        # starts on http://localhost:8000
```

### Key Environment Variables
| Variable | Default | Required? |
|----------|---------|-----------|
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | (empty) | For Azure OCR |
| `AZURE_DOCUMENT_INTELLIGENCE_KEY` | (empty) | For Azure OCR |
| `OCR_MODE` | `auto` | `local` / `azure` / `auto` |
| `API_SECRET_KEY` | (auto-generated) | Set in production |
| `DB_BACKEND` | `sqlite` | `sqlite` / `postgresql` |
| `DEBUG_MODE` | `false` | Enables hot-reload & /docs |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Listen port |
| `LOG_LEVEL` | `INFO` | Console log level |
| `RATE_LIMIT_GENERAL` | `60` | Requests/minute general |
| `RATE_LIMIT_SCAN` | `20` | Requests/minute for scan |
| `CORS_ORIGINS` | localhost:8000,3000 | Comma-separated |

### Cloud-Synced Folder Protection
The project detects if it's in OneDrive/Dropbox/Google Drive and automatically
redirects `uploads/`, `exports/`, and the SQLite database to
`%LOCALAPPDATA%\ReceiptScanner\` to prevent sync corruption.

### Middleware Stack (applied in order)
1. **SecurityHeadersMiddleware** — CSP, X-Content-Type-Options, etc.
2. **RateLimitMiddleware** — sliding window per-IP (60/min general, 20/min scan)
3. **APIKeyMiddleware** — protects destructive endpoints (DELETE, reset, clear cache)
4. **DevTunnelCORSMiddleware** — dynamic CORS for `*.devtunnels.ms`

---

## 🚫 DO NOT TOUCH — Tuned Constants

These values were tuned through real-world testing. Changing them breaks accuracy.

### OCR Engine (`app/ocr/engine.py`)
```
CONFIDENCE_THRESHOLD     = 0.25
PRICE_CONFIDENCE_BOOST   = 0.15
TOTAL_CONFIDENCE_BOOST   = 0.10
LOW_CONFIDENCE_THRESHOLD = 0.4
MIN_TEXT_LENGTH           = 2
```

### Preprocessor (`app/ocr/preprocessor.py`)
```
DENOISE_STRENGTH   = 10       TEMPLATE_WINDOW  = 7
DENOISE_COLOR      = 10       SEARCH_WINDOW    = 21
SHARPEN_KERNEL     = [[-1,-1,-1],[-1,9,-1],[-1,-1,-1]]
ADAPTIVE_BLOCK     = 11       ADAPTIVE_C       = 2
MORPH_KERNEL       = (2,2)
```

### Parser / Validator
```
MAX_SANE_PRICE        = 50000
MAX_SANE_QTY          = 9999
TOTAL_MATCH_TOLERANCE = 5.0
```

### Config Hardcoded Values (in `app/config.py`)
```
OCR_LANGUAGE          = "en"        FUZZY_THRESHOLD        = 0.72
OCR_GPU_ENABLED       = False       FUZZY_MAX_SUGGESTIONS  = 5
IMAGE_MAX_DIMENSION    = 1500       MAX_FILE_SIZE_MB       = 20
IMAGE_JPEG_QUALITY     = 85         ALLOWED_IMAGE_EXTENSIONS = {".jpg",".jpeg",".png",".bmp",".tiff",".webp"}
```

---

## 🔍 PERFORMANCE TARGETS

| Metric | Target | How to Measure |
|--------|--------|---------------|
| First Contentful Paint | < 1.5s | Lighthouse |
| Tab switch response | < 200ms | Manual testing |
| OCR scan (single receipt) | < 30s | Server logs |
| API endpoint response | < 500ms | `/api/health` timing |
| DOM updates (table render) | < 100ms | `performance.now()` |
| Memory (after 50 receipts) | < 200MB | Chrome DevTools |
| SQLite query | < 50ms | Database logs |

---

## 📋 BUG REGISTRY — Learn From History

| # | Bug | Root Cause | File | Rule |
|---|-----|-----------|------|------|
| 1 | XML refused to parse | Double `<?xml?>` declaration | tally_service.py | 5 |
| 2 | Tally crash on Windows | `strftime("%-d")` | tally_service.py | 4 |
| 3 | Catalog search lost sort | Re-fetched instead of cache restore | app.js | 9 |
| 4 | Keystroke fired 5× | `addEventListener` in re-render loop | app.js | 1 |
| 5 | Inline edit stacked | Missing `{ once: true }` | app.js | 2 |
| 6 | Table input duplicated | Per-element listeners rebuilt | app.js | 1 |
| 7 | Excel date inconsistent | `strftime("%d")` gives leading zero | excel_service.py | 4 |
| 8 | Auto-fill listeners leaked | Per-input debounce in re-render | app.js | 1 |
| 9 | Sort clicked = sorted twice | Listener in BOTH global AND render | app.js | 3 |

**Pattern:** 5 of 9 bugs were event listener leaks. **When in doubt, use delegation.**

---

## ⚡ QUICK DECISION TABLE

| Situation | Action |
|-----------|--------|
| Adding click handler to dynamic content | Event delegation on static parent |
| One-time popup/toast handler | `{ once: true }` or remove in handler |
| Formatting date for display | `str(dt.day)`, never `strftime("%-d")` |
| Generating XML | Let `toprettyxml()`/`toxml()` handle declaration |
| Building file path | `pathlib.Path` / `os.path.join()` |
| User clears search/filter | Restore from cached data, don't re-fetch |
| Switching tabs | Abort old controller, create new one |
| Opening modal | Use `showDeleteConfirm`/`showActionConfirm`/`showPromptModal` |
| CSS animation | Only `transform`, `opacity` |
| Writing file | Always `encoding="utf-8"` |
| Catching exception | NEVER `pass` — `logger.exception()` minimum |
| User text → HTML | Always `escHtml()` |
| New API endpoint | `async def`, try/except, `from None`, proper status codes |
| New service | Class + module-level singleton |
| Database change | ALTER TABLE in numbered migration, never DROP |
| Frontend feature | Test all 4 tabs + dark mode + mobile after change |

---

## 🧪 FINAL VERIFICATION CHECKLIST

Run this mentally before delivering ANY change:

- [ ] **Listener audit:** No `addEventListener` inside re-render functions
- [ ] **Date audit:** No `%-d`, `%-m`, `%-H` anywhere in Python
- [ ] **XML audit:** No manual `<?xml?>` before `toprettyxml()` / `toxml()`
- [ ] **Path audit:** No string concatenation for file paths
- [ ] **Error audit:** Every `fetch()` has try/catch, every Python op logs errors
- [ ] **State audit:** Cached data restored after filter/search clear
- [ ] **Abort audit:** Long-running fetches accept and respect `signal`
- [ ] **HTML audit:** User-provided strings go through `escHtml()`
- [ ] **Timer audit:** Intervals cleared in error AND success paths
- [ ] **Import audit:** New imports at file top, not inline
- [ ] **Cache version:** If JS/CSS/HTML changed, bump `?v=` in index.html
- [ ] **Windows test:** No Unix-only assumptions (`%-d`, `/` paths, etc.)
- [ ] **Tests pass:** `pytest tests/ -v` — all green
- [ ] **Lint clean:** `ruff check app/` — no warnings
- [ ] **Syntax valid:** `py_compile.compile()` on all changed `.py` files
- [ ] **Server starts:** `python run.py` launches without errors
- [ ] **Manual smoke test:** Feature works in browser, all 4 tabs load, dark mode OK

---

## 📁 FILE MAP — Where Things Live

```
DEVELOPMENT_GUIDE.md   — THIS FILE: rules, architecture, bug registry
OPTIMIZATION_HISTORY.md — Performance engineering log: all 36 optimizations,
                          timing data, bottleneck analysis, tuned constants.
                          ⚠ READ BEFORE ANY PERFORMANCE WORK.
app/
  main.py              — FastAPI app, startup/shutdown lifecycle
  config.py            — ALL constants, env vars (300 lines)
  database.py          — SQLite pool, migrations, CRUD (1300+ lines)
  middleware.py         — Security headers, rate limit, API key, CORS
  api/routes.py        — ALL 40 endpoints + Pydantic models
  ocr/
    engine.py          — EasyOCR wrapper, confidence scoring
    azure_engine.py    — Azure Document Intelligence
    hybrid_engine.py   — Dual-pass local+cloud orchestration
    parser.py          — Receipt text → structured data
    preprocessor.py    — Image enhancement (denoise, sharpen, threshold)
    validators.py      — Price/quantity/total sanity checks
    quality_scorer.py  — Image quality A-F grading
    image_cache.py     — LRU cache for OCR results
    usage_tracker.py   — Azure API daily/monthly limits
    total_verifier.py  — Bill total cross-check
  services/
    batch_service.py      — Concurrent batch OCR
    correction_service.py — Smart OCR learning
    dedup_service.py      — Duplicate detection
    excel_service.py      — Excel reports (openpyxl)
    product_service.py    — Product catalog CRUD
    receipt_service.py    — Receipt CRUD + items
    tally_service.py      — Tally XML/JSON export
  static/
    index.html            — SPA shell (~1,100 lines)
    app.js                — ALL frontend logic (~5,700 lines)
    styles.css            — ALL styles (~4,700 lines)
  training/              — OCR training & benchmarking
tests/
  test_api.py            — API endpoint tests
  test_app.py            — App integration tests
  test_services.py       — Service unit tests
  test_preprocessing.py  — Image processing tests
  test_parser_internals.py — Parser logic tests
  test_smart_ocr.py      — Smart OCR feature tests
  (+ more specialized test files)
```

---

## 🔒 SECURITY NOTES

- `API_SECRET_KEY` auto-generated if not set — always set in production
- File uploads validated: type, size (20MB max), extension whitelist
- SQL queries use **parameterized statements** — NEVER string interpolation
- CORS restricted to configured origins — tighten in production
- Export files served from `EXPORT_DIR` with path traversal protection
- Rate limiting: 60/min general, 20/min for scan endpoints
- Security headers: CSP, no-sniff, referrer-policy on all responses
- Destructive endpoints protected by API key middleware

---

*Last updated: 2026-03-19 — After fixing 9 production bugs*
*Guide version: 3.0 — Complete agent instruction manual*
