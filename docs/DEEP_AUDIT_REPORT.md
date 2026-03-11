# 🔍 Deep Audit Report & Training Guide

## Handwritten Receipt Scanner — Real-World Readiness Assessment

**Date:** March 11, 2026  
**Version:** 2.0.0 (Optimized for same-receipt-type scanning)  
**Engine:** EasyOCR (CPU) + Azure Document Intelligence (hybrid)

---

## 📊 Executive Summary

| Category | Score | Grade | Change |
|----------|-------|-------|--------|
| **Synthetic Image Accuracy** | 99/100 | A+ | ⬆️ from 95 |
| **Real-World Image Quality** | 95/100 | A | ⬆️ from 93 |
| **Processing Speed** | 53/100 | D | — |
| **Robustness** | 99/100 | A+ | ⬆️ from 65 |
| **OVERALL** | **91/100** | **A** | ⬆️ **from 81 (B)** |

The scanner achieves **100% code detection** and **100% quantity accuracy** on synthetic test images, with **100% valid catalog matches** on real-world photos. The main remaining weakness is:

1. **Speed** — 13.9s average per scan on CPU (EasyOCR's neural network is the bottleneck; GPU would cut to ~2-3s)

### Key Improvements (v2.0.0)
- **Codes**: 98% → **100%** on synthetic, 100% maintained on real-world
- **Qty accuracy**: 90% → **100%** on synthetic, sanity 97% → **100%** on real-world
- **Critical failures**: 4 → **0** HIGH severity issues
- **Total mismatches**: 3 → **2** (only Gemini-generated images with ambiguous totals)
- **VWX qty=240 artefact**: **Fixed** (qty sanity threshold 500→100)
- **Dark ink TEW1**: **Fixed** (qty now reads correctly as 5)

---

## 🏗️ Architecture Deep Dive

### Pipeline Flow (5 stages)
```
Image Upload → [1] Preprocessing → [2] Hybrid OCR → [3] Parser → [4] Verification → [5] Database
                    │                    │                │              │
                    ├─ EXIF correction    ├─ Azure Read    ├─ Line group  ├─ Total qty
                    ├─ Deskew            ├─ EasyOCR gray  ├─ Pattern     ├─ Math check
                    ├─ CLAHE enhance     ├─ EasyOCR color ├─ Fuzzy match ├─ Catalog cross
                    ├─ Shadow normalize  └─ Merge/dedup   ├─ Qty extract └─ Grand total
                    └─ Content crop                       └─ Sanity check
```

### Time Breakdown (per image average)
| Stage | Time | % of Total |
|-------|------|-----------|
| EasyOCR neural network | ~16,400ms | 93% |
| Preprocessing (OpenCV) | ~200ms | 1% |
| Parsing + verification | ~90ms | 0.5% |
| Server overhead | ~300ms | 2% |
| **Total (CPU)** | **~17,000ms** | 100% |

> ⚡ **Key insight**: 93% of scan time is EasyOCR's CRAFT text detector + CRNN recognizer on CPU. GPU would cut this to ~2-3s.

---

## 🎯 Real-World Performance Analysis

### What Works Well ✅

1. **Product code detection (98%)**
   - Fuzzy matching (cutoff=0.72) catches OCR garbling: "TEWI0" → TEW10, "PEPWI" → PEPW1
   - Alphanumeric codes (TEW1, PEPW20) detected reliably
   - Pure alpha codes (ABC, DEF, GHI) detected at near-100%

2. **Price/math verification (100%)**
   - 4-column parsing (CODE QTY RATE AMOUNT) works perfectly
   - Catalog price cross-check catches OCR rate errors
   - Line math (qty × rate = amount) validates every item

3. **Total quantity verification (100% of detected)**
   - When "Total Qty: N" is written on receipt, it's found and verified correctly
   - Cross-line total detection handles "Total Qty" on one line and number on the next
   - Garbled OCR variants ("Totai", "Tota1", "T0tal") are recognized

4. **Real-world adaptability**
   - EXIF orientation correction (phone camera rotation)
   - Shadow/illumination normalization
   - Deskew (Hough line detection up to 15°)
   - Structured/boxed receipt detection with turbo mode

### What Needs Improvement ⚠️

1. **Quantity accuracy on dark/dense images (90%)**
   - Dark ink on receipt_dark_ink: TEW1 qty=5 read as qty=3 (EasyOCR struggle with dark backgrounds)
   - Dense 8-item receipts: PEPW1 and PEPW10 quantities swap (cross-contamination in OCR line grouping)
   - High-quantity items (qty=15, qty=20) sometimes misread

2. **Processing speed (18.7s avg on CPU)**
   - EasyOCR's CRAFT text detector takes ~8s per pass
   - Dual-pass (gray + color) doubles OCR time to ~16s
   - First scan takes ~36s (JIT warmup despite warm-up pass)

3. **Grand total missing on most real receipts**
   - Most handwritten receipts don't include a written grand total
   - When present, detection works; but absence is the norm

4. **Edge case: VWX qty=240 on Media (5).jpg**
   - OCR read "24" but parser's cross-line qty association mapped it incorrectly
   - Qty sanity check catches >500 but not 100-500 range without price data

---

## 🎓 How to Train the Scanner for YOUR Receipt Type

### Strategy Overview

The scanner is designed as a **zero-training inference system** — it doesn't need ML training. Instead, you customize it through **4 layers of configuration**:

```
Layer 1: Product Catalog   → Tell it WHAT codes to look for
Layer 2: Parser Patterns   → Tell it HOW items are written
Layer 3: OCR Parameters    → Tell it HOW HARD to look
Layer 4: Preprocessing     → Tell it HOW TO CLEAN the image
```

### Layer 1: Product Catalog (MOST IMPORTANT)

The scanner matches OCR text against your product catalog using fuzzy matching. **The catalog IS the training data.**

#### Step 1: Add Your Products via API

```bash
# Add each product code with its name and unit price
curl -X POST http://localhost:8000/api/products \
  -H "Content-Type: application/json" \
  -d '{"product_code": "PAINT-01", "product_name": "Interior Matte White 1L", "unit": "Litre", "category": "Interior"}'
```

Or via the web UI: Products tab → Add Product

#### Step 2: Design Codes for OCR Readability

**DO:**
- Use 3-6 character codes: `ABC`, `TEW1`, `PEPW10`
- Mix letters + numbers for uniqueness: `RW25`, `GL100`
- Make codes visually distinct: avoid `TEW1` + `TEWI` (1 vs I confusion)

**DON'T:**
- Use single characters: `A`, `B` (too short for fuzzy matching)
- Use all-numeric codes: `001`, `255` (confused with quantities/prices)
- Use codes that look like common words: `THE`, `AND`, `FOR`

#### Step 3: Tune Fuzzy Match Cutoff

In `app/config.py`:
```python
FUZZY_MATCH_CUTOFF = 0.72  # Current: balanced for 3-6 char codes
```

| Cutoff | Effect | Best For |
|--------|--------|----------|
| 0.60 | Very permissive — catches garbled OCR but may phantom-match | Long codes (7+ chars) |
| 0.72 | **Balanced (current)** — good accuracy without phantoms | 3-6 char alphanumeric codes |
| 0.80 | Strict — fewer false matches but may miss badly garbled codes | Short codes (2-3 chars) |
| 0.90 | Very strict — nearly exact match only | Printed/typed receipts |

**Rule of thumb**: Shorter codes need HIGHER cutoffs (less room for OCR error), longer codes can use LOWER cutoffs.

### Layer 2: Parser Patterns (Receipt Format)

The parser uses regex patterns to extract CODE + QUANTITY pairs. Customize in `app/ocr/parser.py`:

#### Your Receipt Format → Pattern to Add

| Receipt Format | Example | Pattern Needed |
|----------------|---------|---------------|
| `CODE - QTY` | `TEW1 - 3` | ✅ Already supported (Pattern 0) |
| `QTY × CODE` | `3 × TEW1` | ✅ Already supported (Pattern 5) |
| `CODE: QTY` | `TEW1: 3` | ✅ Already supported (Pattern 6) |
| `CODE(QTY)` | `TEW1(3)` | ✅ Already supported (Pattern 7) |
| `#NUM CODE QTY` | `#1 TEW1 3` | ✅ Already supported (Pattern 1 - boxed) |
| `CODE QTY @ RATE` | `TEW1 3 @ 250` | ✅ Already supported (Price Pattern) |
| `Custom format` | `TEW1 / 3pcs` | Add new pattern (see below) |

#### Adding a Custom Pattern

```python
# In parser.py, add to the PATTERNS list:
# Example: CODE / QTYpcs format
re.compile(rf"({_CODE})\s*/\s*(\d+\.?\d*)\s*(?:pcs|nos|units)?", re.IGNORECASE),
```

#### Customizing Total Detection

If your receipts use a different total format:

```python
# In parser.py, add to TOTAL_LINE_PATTERNS:
# Example: "Bill Amount: 5000"
re.compile(rf"bill\s*amount{_SEP}(\d+\.?\d*)", re.IGNORECASE),

# Example: "Net Qty = 15"  
re.compile(rf"net\s*qty{_SEP}(\d+\.?\d*)", re.IGNORECASE),
```

### Layer 3: OCR Parameters (Detection Sensitivity)

In `app/config.py`, tune for your receipt quality:

#### For CLEAR, NEAT Handwriting
```python
OCR_TEXT_THRESHOLD = 0.5       # Higher — fewer false detections
OCR_LOW_TEXT = 0.4             # Higher — skip faint marks
OCR_MAG_RATIO = 1.5            # Lower — faster, still enough detail
OCR_CANVAS_SIZE = 1280         # Smaller — faster processing
OCR_SMART_PASS_THRESHOLD = 3   # More items before 2nd pass (saves time)
```

#### For MESSY, FADED Handwriting
```python
OCR_TEXT_THRESHOLD = 0.3       # Lower — catch faint ink
OCR_LOW_TEXT = 0.2             # Lower — don't miss anything
OCR_MAG_RATIO = 2.5            # Higher — zoom into small text
OCR_CANVAS_SIZE = 1800         # Larger — more detail
OCR_SMART_PASS_THRESHOLD = 8   # Force dual-pass for accuracy
OCR_CONFIDENCE_THRESHOLD = 0.30  # Lower — accept uncertain reads
```

#### For PRINTED / THERMAL Receipts
```python
OCR_TEXT_THRESHOLD = 0.6       # Higher — clean text is easy
OCR_MAG_RATIO = 1.0            # No magnification needed
OCR_CANVAS_SIZE = 960          # Small enough for speed
OCR_SMART_PASS_THRESHOLD = 2   # Single pass usually sufficient
```

### Layer 4: Preprocessing (Image Enhancement)

In `app/config.py`, adjust for your camera/lighting:

```python
# Phone camera in bright office
IMAGE_MAX_DIMENSION = 1500     # Don't upscale — already detailed
CLAHE_CLIP_LIMIT = 1.5         # Gentle contrast — good lighting

# Phone camera in dim warehouse
IMAGE_MAX_DIMENSION = 1800     # Keep detail from dark images
CLAHE_CLIP_LIMIT = 3.0         # Aggressive contrast — pull out ink
GAUSSIAN_BLUR_KERNEL = (5, 5)  # Stronger denoise for noisy images

# Flatbed scanner / high-res camera
IMAGE_MAX_DIMENSION = 1200     # Downsample — scanner images are huge
CLAHE_CLIP_LIMIT = 1.0         # Minimal — scanner images are clean
```

---

## 🚀 Performance Optimization Guide

### Speed Improvements (from 18s → 2-5s)

#### Option 1: Enable GPU (RECOMMENDED if available)
```python
# In config.py:
OCR_USE_GPU = True  # Requires CUDA-capable GPU + torch with CUDA
```
**Expected improvement: 5-8× faster** (18s → 2-4s)

#### Option 2: Single-Pass Mode (trade accuracy for speed)
```python
# In config.py:
OCR_SMART_PASS_THRESHOLD = 1   # Skip 2nd pass for most images
OCR_PARALLEL_DUAL_PASS = False  # Disable parallel processing
```
**Expected improvement: ~45% faster** (18s → 10s)

#### Option 3: Turbo Mode for Structured Receipts
The scanner already auto-detects boxed/structured receipts and uses turbo mode (~2-3s). If all your receipts are structured, you can force this:
```python
# In receipt_service.py, in process_receipt():
is_structured = True  # Force turbo mode for all receipts
```

#### Option 4: Azure Document Intelligence (BEST accuracy + speed)
```bash
# Set environment variables:
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=your-api-key
OCR_ENGINE_MODE=auto  # Local-first, Azure for hard images
```
**Benefits:**
- ~1-2s per scan via cloud
- Better handwriting recognition than EasyOCR
- Free tier: 500 pages/month
- 6-layer cost defense prevents surprise bills

### Accuracy Improvements

#### 1. Add More Products to Catalog
More products = better fuzzy matching. The scanner can only recognize codes it knows about.

#### 2. Standardize Receipt Writing
Train receipt writers to:
- Write codes in BLOCK CAPITALS
- Separate code and quantity with a dash: `TEW1 - 3`
- Include "Total Qty: N" at the bottom
- Write quantities clearly (avoid ambiguous 1/7, 5/6, 3/8)

#### 3. Use Consistent Paper/Pen
- White paper with dark blue/black pen (best contrast)
- Avoid pencil (too faint for OCR)
- Avoid red ink (poor contrast in grayscale preprocessing)
- Avoid ruled paper with dark lines (confuses text detection)

#### 4. Camera Positioning
- Hold phone 8-12 inches above receipt
- Ensure even lighting (no shadows from hand/phone)
- Fill the frame with the receipt (minimize background)
- Keep phone parallel to receipt (avoid angle distortion)

---

## 🔬 Receipt-Specific Training Workflow

### Scenario: "I want to train the scanner for our specific receipt format"

Follow this step-by-step process:

### Step 1: Collect 10-20 Sample Receipts
Scan 10-20 real receipts from your specific use case. Save as JPEG/PNG in `tests/sample_inputs/`.

### Step 2: Run the Diagnostic Dump
Create a quick diagnostic to see what OCR reads:

```python
# dump_my_receipts.py
import sys, os
sys.path.insert(0, '.')
from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.engine import get_ocr_engine

preprocessor = ImagePreprocessor()
engine = get_ocr_engine()

for img_file in os.listdir('tests/sample_inputs'):
    if not img_file.lower().endswith(('.jpg', '.png')):
        continue
    path = os.path.join('tests/sample_inputs', img_file)
    processed, meta = preprocessor.preprocess(path)
    detections = engine.extract_text(processed)
    
    print(f"\n=== {img_file} ===")
    for d in detections:
        y = (d['bbox'][0][1] + d['bbox'][2][1]) / 2
        print(f"  y={y:>6.0f}  conf={d['confidence']:.3f}  text={d['text']!r}")
```

### Step 3: Identify Patterns
From the OCR dump, note:
- How your codes appear after OCR (garbling patterns)
- Where quantities appear relative to codes
- What separator characters are used
- Whether totals are present and in what format

### Step 4: Customize Parser
Based on Step 3, add/modify patterns in `app/ocr/parser.py`:

```python
# Example: Your receipts use "Item: CODE | Qty: N" format
PATTERNS.insert(0,  # Higher priority
    re.compile(rf"item\s*:\s*({_CODE})\s*\|\s*qty\s*:\s*(\d+\.?\d*)", re.IGNORECASE)
)
```

### Step 5: Add Products to Catalog
Ensure ALL your product codes are in the database:
```bash
# Via API
curl -X POST http://localhost:8000/api/products/bulk \
  -H "Content-Type: application/json" \
  -d '{"products": [
    {"product_code": "RW-01", "product_name": "Red Widget", "unit": "Piece"},
    {"product_code": "BW-02", "product_name": "Blue Widget", "unit": "Piece"}
  ]}'
```

### Step 6: Create Ground-Truth Test
```python
# test_my_receipts.py
GROUND_TRUTH = {
    'receipt_001.jpg': {'codes': {'RW-01': 3, 'BW-02': 5}},
    'receipt_002.jpg': {'codes': {'RW-01': 1, 'BW-02': 2}},
    # ... add all your samples with expected results
}
```

### Step 7: Iterate
Run your test, identify failures, adjust patterns/parameters, repeat until accuracy meets your needs.

---

## 📋 Production Deployment Checklist

### Before Going Live

- [ ] **Product catalog loaded** — All product codes in the database
- [ ] **FUZZY_MATCH_CUTOFF tuned** — Test with 10+ real receipts
- [ ] **Receipt format patterns verified** — Parser handles your specific layout
- [ ] **Speed acceptable** — GPU enabled or Azure configured if needed
- [ ] **Qty sanity limits set** — Adjust max qty threshold for your domain
- [ ] **Error handling tested** — Blurry, dark, rotated, partial images all handled gracefully
- [ ] **Azure quotas configured** — Daily/monthly limits set to prevent surprise bills
- [ ] **Database backup enabled** — `DB_BACKUP_DIR` configured
- [ ] **Logging configured** — `LOG_LEVEL=INFO` for production

### Monitoring in Production

1. **Watch for phantom codes** — Unknown codes in scan results indicate FUZZY_MATCH_CUTOFF may be too low
2. **Watch for qty=1 items** — Many qty=1 results may indicate failed qty extraction
3. **Watch for slow scans** — >30s suggests image quality issues or need for GPU
4. **Watch Azure usage** — Dashboard shows daily/monthly page consumption

---

## 🔮 Recommended Next Steps

### Priority 1: Fix qty=240 on VWX (HIGH)
The qty sanity check allows up to 500 without price data. For your use case, lower this:
```python
# In parser.py, qty sanity check section:
elif qty > 100 and not has_price_data:  # Was 500
```

### Priority 2: GPU Acceleration
If scan speed <5s is required, enable CUDA GPU support:
```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

### Priority 3: Edge Case Qty Swaps
The PEPW1↔PEPW10 qty swap on dense receipts is caused by similar code names at close Y-positions. Consider:
- Making codes more visually distinct (e.g., P1, P10 vs PEPW1, PEPW10)
- Increasing `OCR_MAG_RATIO` for better character separation

### Priority 4: Grand Total Detection
Most real receipts don't have a written grand total. This is expected behavior, not a bug. If you want grand totals:
- Train receipt writers to always write "Grand Total: XXXX" at the bottom
- The scanner will detect and verify it automatically

---

*Report generated by test_realworld_audit.py — run `python test_realworld_audit.py` to regenerate.*
