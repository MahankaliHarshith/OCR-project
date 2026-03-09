# Receipt Design & Scanning Optimization Guide

## How to Achieve Near-100% OCR Accuracy and Maximum Speed

This document provides a complete set of recommendations for **receipt template design**,
**handwriting rules**, and **scanner-side optimizations** that, when applied together,
can push OCR accuracy from the current ~88% to **95–99%** and cut processing time
by another **40–60%**.

---

## Table of Contents

1. [Current Bottlenecks](#1-current-bottlenecks)
2. [Optimized Receipt Template Design](#2-optimized-receipt-template-design)
3. [Handwriting Rules for Writers](#3-handwriting-rules-for-writers)
4. [Scanner-Side Optimizations](#4-scanner-side-optimizations)
5. [Implementation Roadmap](#5-implementation-roadmap)
6. [Expected Impact Summary](#6-expected-impact-summary)

---

## 1. Current Bottlenecks

| Problem | Root Cause | Current Accuracy Impact |
|---------|-----------|------------------------|
| Line numbers confused with quantities | `5 . MNO` → scanner thinks qty=5, but 5 is line number | ~12% qty errors |
| QT markers mangled by OCR | `2QT` read as `Iop}`, `3QT` read as `'tt` | ~8% qty errors |
| Code letters misread | `GHI` read as `6nl`, `ABC` read as `ALC` | Recovered by fuzzy matching, but adds latency |
| Adjacent detections grouped wrongly | Qty marker `Iop}` at y=756 sits between JKL (y=712) and MNO (y=800) | ~5% qty errors |
| Full image scanned | Blank margins, headers, and footers are processed unnecessarily | ~30% wasted OCR time |
| Two OCR passes needed | First pass often finds too few items, triggering expensive second pass | ~50% of total time |

**Key Insight**: Most errors come from the scanner not knowing WHERE the code is
and WHERE the quantity is. A structured receipt template eliminates this ambiguity entirely.

---

## 2. Optimized Receipt Template Design

### 2.1 The Core Idea: Boxed Grid Layout

Instead of free-form handwriting on blank paper, use a **pre-printed receipt template**
with clearly defined boxes/cells for each field.

```
┌─────────────────────────────────────────────────────────┐
│                    RECEIPT                               │
│                                                         │
│  Date: ___/___/______          Receipt #: ____________  │
│                                                         │
├─────┬────────────────┬───────────────┬──────────────────┤
│ S.No│   PRODUCT CODE │   QUANTITY    │   UNIT           │
├─────┼────────────────┼───────────────┼──────────────────┤
│  1  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  2  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  3  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  4  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  5  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  6  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  7  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  8  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│  9  │ [___________]  │ [_________]   │ [____________]   │
├─────┼────────────────┼───────────────┼──────────────────┤
│ 10  │ [___________]  │ [_________]   │ [____________]   │
├─────┴────────────────┴───────────────┴──────────────────┤
│                                                         │
│  Total Items: [_________]                               │
│  Prepared By: ________________                          │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Why Boxes Work

| Benefit | Explanation |
|---------|-------------|
| **Eliminates line-number confusion** | S.No is PRE-PRINTED (not handwritten), so the scanner never confuses `5` (line#) with the quantity |
| **Spatial separation** | Code and Quantity are in SEPARATE BOXES — the scanner knows exactly which region contains what |
| **No QT suffix needed** | Writers no longer need to write `2QT` after the number — the box label tells the scanner it's a quantity |
| **Consistent Y-coordinates** | All cells in the same row share the same Y range — no ambiguous grouping |
| **Reduced scan area** | Scanner only needs to OCR the CODE and QTY columns, skipping S.No (pre-printed) and blank rows |
| **Faster OCR** | Each box is a small, isolated region — OCR processes tiny crops instead of the full image |

### 2.3 Recommended Dimensions (for A5 / Half-Letter paper)

```
┌──────────────────────────────────────────┐
│           Page: A5 (148mm × 210mm)       │
│                                          │
│  Margins: 10mm all sides                 │
│  Usable area: 128mm × 190mm             │
│                                          │
│  Header area: 128mm × 25mm              │
│  Table area:  128mm × 150mm             │
│  Footer area: 128mm × 15mm             │
│                                          │
│  Column widths:                          │
│    S.No:         15mm (pre-printed)      │
│    Product Code: 45mm (handwritten)      │
│    Quantity:     30mm (handwritten)       │
│    Unit:         38mm (handwritten/pre)   │
│                                          │
│  Row height: 12-15mm                     │
│    (fits comfortable handwriting)        │
│                                          │
│  Box border: 0.5mm black line            │
│  Box fill: light gray (#F0F0F0) or white │
│                                          │
│  Max rows: 10 items per receipt          │
│  (use a second receipt for more)         │
└──────────────────────────────────────────┘
```

### 2.4 Color-Coded Columns (Advanced)

Use light background colors to help the scanner identify columns even faster:

| Column | Background Color | Purpose |
|--------|-----------------|---------|
| S.No | Light Blue `#E3F2FD` | Pre-printed, scanner ignores this |
| Product Code | Light Yellow `#FFFDE7` | Scanner targets ONLY this for code extraction |
| Quantity | Light Green `#E8F5E9` | Scanner targets ONLY this for number extraction |
| Unit | Light Gray `#F5F5F5` | Optional, low priority |

**Scanner logic**: Detect the green column → extract digits only → that's the quantity.
Detect the yellow column → extract letters only → that's the product code.

### 2.5 Corner Markers / Fiducial Points

Add **four corner markers** (like QR code finder patterns) to the receipt template:

```
  ■ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ■
  │                                         │
  │         (receipt content)               │
  │                                         │
  ■ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ■
```

**Benefits:**
- Scanner instantly detects the receipt boundaries (no blank margin scanning)
- Enables **perspective correction** (if photo is taken at an angle)
- Enables **rotation correction** (if receipt is sideways/upside down)
- Allows precise column coordinate calculation relative to the markers

### 2.6 QR Code / Barcode Header (Optional but Powerful)

Add a small QR code or barcode in the header that encodes:
- Template version (so scanner knows the exact column layout)
- Receipt serial number
- Date (pre-filled)

```
┌─────────────────────────────────────────┐
│  ┌─────┐                               │
│  │ QR  │  RECEIPT  #000142              │
│  │CODE │  Date: 22/02/2026              │
│  └─────┘  Template: v2.1               │
├─────┬──────────┬──────────┬─────────────┤
│ ... │  CODE    │   QTY    │   UNIT      │
```

**Scanner logic**: Read QR first → know exact pixel coordinates for every cell →
crop each cell → OCR only the handwritten content. This makes scanning **deterministic**
instead of heuristic.

---

## 3. Handwriting Rules for Writers

Even with the best template, sloppy handwriting kills accuracy.
These rules should be printed on the back of every receipt pad.

### 3.1 Golden Rules (Print These on the Receipt Pad)

```
╔══════════════════════════════════════════════════════╗
║              WRITING INSTRUCTIONS                    ║
╠══════════════════════════════════════════════════════╣
║                                                      ║
║  1. USE BLOCK CAPITALS ONLY                          ║
║     ✅  A B C    ❌  a b c                           ║
║     ✅  M N O    ❌  m n o                           ║
║                                                      ║
║  2. WRITE ONE CHARACTER PER SEGMENT                  ║
║     ✅  |A|B|C|  (spaced out, clear)                ║
║     ❌  ABC      (cramped, letters merge)            ║
║                                                      ║
║  3. NUMBERS: WRITE CLEARLY, NO LETTERS               ║
║     ✅  10       ❌  IO  (looks like I-O)            ║
║     ✅  2        ❌  Z   (looks like 2)              ║
║     ✅  5        ❌  S   (looks like 5)              ║
║                                                      ║
║  4. USE DARK PEN (black or dark blue)                ║
║     ✅  Black ballpoint                              ║
║     ❌  Pencil, light blue, red                      ║
║                                                      ║
║  5. STAY INSIDE THE BOX                              ║
║     ✅  Text centered in the cell                    ║
║     ❌  Text overlapping borders                     ║
║                                                      ║
║  6. CROSS MISTAKES CLEANLY                           ║
║     ✅  Single horizontal line through error         ║
║     ❌  Scribbling over the mistake                  ║
║                                                      ║
║  7. DO NOT WRITE "QT" AFTER QUANTITIES               ║
║     The Quantity column already means "quantity"      ║
║     ✅  10       ❌  10QT                            ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
```

### 3.2 Character Clarity Guide

These specific characters cause the most OCR confusion.
Train writers to distinguish them:

| Confusing Pair | How to Write Clearly |
|---------------|---------------------|
| `1` vs `I` vs `l` | Write `1` with a **horizontal base**: `1̲` . Write `I` with **serifs**: `I` |
| `0` vs `O` | Write `0` as **narrow oval**. Write `O` as **wide circle** |
| `5` vs `S` | Write `5` with a **sharp angle** at top. Write `S` with **curves** |
| `2` vs `Z` | Write `2` with a **curved top**. Write `Z` with **straight lines** |
| `6` vs `G` | Write `6` with a **closed loop**. Write `G` with a **horizontal bar** |
| `8` vs `B` | Write `8` as **two stacked circles**. Write `B` with **flat left side** |
| `9` vs `g` vs `q` | Write `9` with **tail going straight down** |
| `D` vs `0` | Write `D` with a **flat left edge** |

### 3.3 Recommended Pen Specifications

| Property | Recommended | Avoid |
|----------|------------|-------|
| **Color** | Black or Dark Blue | Red, Green, Light Blue, Pencil |
| **Tip** | Ballpoint 0.7mm–1.0mm | Fine tip < 0.5mm (too thin for camera) |
| **Type** | Ballpoint or Gel | Felt-tip (bleeds), Fountain (variable width) |
| **Pressure** | Firm, consistent | Light, scratchy strokes |

---

## 4. Scanner-Side Optimizations

### 4.1 Box Detection Pipeline (NEW — Biggest Speed Win)

Instead of OCR-ing the entire image, detect the grid/table structure first,
then OCR only the relevant cells.

```
  CURRENT PIPELINE (slow):
  ┌─────────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐
  │  Load   │ →  │ Preproc  │ →  │ OCR Full  │ →  │  Parse   │
  │  Image  │    │ (resize, │    │  Image    │    │  Text    │
  │         │    │  gray)   │    │ (~15sec)  │    │         │
  └─────────┘    └──────────┘    └───────────┘    └──────────┘


  PROPOSED PIPELINE (fast):
  ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐    ┌──────────┐
  │  Load   │ →  │ Detect   │ →  │ Crop     │ →  │ OCR     │ →  │ Direct   │
  │  Image  │    │ Grid /   │    │ CODE &   │    │ Small   │    │ Map      │
  │         │    │ Boxes    │    │ QTY      │    │ Cells   │    │ (no      │
  │         │    │ (~200ms) │    │ Cells    │    │ (~2sec) │    │ parsing) │
  └─────────┘    └──────────┘    └──────────┘    └─────────┘    └──────────┘
```

**How Box Detection Works (OpenCV):**

```
Step 1: Convert to grayscale
Step 2: Apply adaptive threshold (to find dark lines on light paper)
Step 3: Detect horizontal lines using morphological operations
         kernel = cv2.getStructuringElement(MORPH_RECT, (40, 1))
Step 4: Detect vertical lines
         kernel = cv2.getStructuringElement(MORPH_RECT, (1, 40))
Step 5: Combine horizontal + vertical → grid mask
Step 6: Find contours of each cell
Step 7: Sort cells by (row, column) position
Step 8: Crop each cell → send ONLY code & qty cells to OCR
```

**Speed Impact:**
- Current: OCR processes ~1.3 megapixels (1280×1024) → ~6–8 seconds per pass
- Proposed: OCR processes ~10 cells × ~5,000 pixels each = ~50,000 pixels → **< 1 second**
- That's a **10–15x speed improvement** for OCR alone

### 4.2 Column-Aware Parsing (No Guessing)

With box detection, the scanner KNOWS which column a cell belongs to:

```python
# CURRENT approach (guessing):
# "4 JkL Iop} ." → is 4 the line number? the quantity? part of the code?

# PROPOSED approach (deterministic):
# Cell at column=1 (CODE): "JKL"  → product code (letter-only OCR)
# Cell at column=2 (QTY):  "2"    → quantity (digit-only OCR)
# No ambiguity. No QT markers. No line numbers.
```

**Key: Use different OCR modes per column:**

| Column | OCR Mode | Allowed Characters | Example |
|--------|---------|-------------------|---------|
| Product Code | `allowlist='ABCDEFGHIJKLMNOPQRSTUVWXYZ'` | Letters only | `JKL` |
| Quantity | `allowlist='0123456789.'` | Digits only | `10` |

By restricting the character set, OCR accuracy increases dramatically because:
- In the CODE column, `6` can ONLY be `G`, `0` can ONLY be `O`
- In the QTY column, `O` can ONLY be `0`, `I` can ONLY be `1`
- No more confusion between letters and digits!

### 4.3 Corner Marker Detection

```
Algorithm:
1. Convert to grayscale
2. Apply binary threshold
3. Find contours with area > 100px and < 2000px
4. Filter for square-ish contours (aspect ratio 0.8–1.2)
5. Find 4 contours closest to the 4 image corners
6. Compute perspective transform from these 4 points
7. Warp image to perfect rectangle
8. Now grid detection is trivially accurate
```

**Accuracy Impact**: Eliminates ALL rotation/perspective distortion problems.

### 4.4 Empty Row Detection (Skip Blank Rows)

```
Algorithm:
1. After cropping a cell, compute its mean pixel intensity
2. If mean > 240 (nearly white), the cell is EMPTY → skip it
3. Only OCR cells with actual ink (mean < 230)

Speed Impact: If a 10-row receipt has only 4 items filled,
              we skip 6 empty rows → 60% fewer OCR calls
```

### 4.5 Checksum / Validation Row

Add a **"Total Items"** field at the bottom of the receipt:

```
├─────┴────────────────┴───────────────┴──────────────────┤
│  Total Items: [ 5 ]                                     │
└─────────────────────────────────────────────────────────┘
```

**Scanner logic:**
1. OCR all item rows → found 5 items
2. OCR the "Total Items" cell → reads `5`
3. If they match → **high confidence, no review needed**
4. If they don't match → flag for manual review

This acts as a **self-check** built into the receipt itself.

---

## 5. Implementation Roadmap

### Phase 1: Quick Wins (No Template Change)

These can be implemented in the scanner software immediately:

| Change | Effort | Accuracy Gain | Speed Gain |
|--------|--------|---------------|------------|
| Print writing instructions on receipt pad back | 1 day | +5–10% | – |
| Mandate black ballpoint pens | 0 days | +3–5% | – |
| Mandate BLOCK CAPITALS | 0 days | +5–8% | – |
| Remove QT suffix requirement (just write the number) | 0 days | +5% | – |

### Phase 2: Template Introduction (1–2 Weeks)

| Change | Effort | Accuracy Gain | Speed Gain |
|--------|--------|---------------|------------|
| Design and print boxed grid receipt template | 3 days | – | – |
| Add corner markers to template | 1 day | – | – |
| Add column headers and color coding | 1 day | – | – |
| Implement box/grid detection in scanner | 5 days | +15–20% | +60–70% |
| Implement column-aware OCR (letter/digit restriction) | 2 days | +10–15% | +10% |
| Implement empty row skipping | 1 day | – | +20–40% |

### Phase 3: Advanced Features (2–4 Weeks)

| Change | Effort | Accuracy Gain | Speed Gain |
|--------|--------|---------------|------------|
| QR code header with template version | 3 days | +2% | +5% |
| Corner marker perspective correction | 3 days | +5% | – |
| Total Items checksum validation | 1 day | +3% | – |
| Per-cell confidence scoring | 2 days | Better review flagging | – |
| Training custom OCR model on receipt handwriting | 2 weeks | +10–15% | – |

---

## 6. Expected Impact Summary

### Current Performance (Free-Form Receipts)

| Metric | Current Value |
|--------|:------------:|
| Code Accuracy | 100% (16/16) |
| Quantity Accuracy | 88% (14/16) |
| Avg Time Per Image | ~15 seconds |
| OCR Passes Needed | 2 (gray + color) |
| Main Error Source | Line number ↔ quantity confusion |

### Projected Performance (Boxed Template + All Optimizations)

| Metric | Projected Value | Improvement |
|--------|:--------------:|:-----------:|
| Code Accuracy | **99–100%** | +0% (already at ceiling) |
| Quantity Accuracy | **97–100%** | +10–12% |
| Avg Time Per Image | **1–3 seconds** | **5–15x faster** |
| OCR Passes Needed | **1 (cells only)** | 50% fewer |
| Main Error Source | Illegible handwriting only | Eliminated structural errors |

### Why the Improvement is So Large

```
  CURRENT: Scan everything → guess what's code vs qty vs line# vs noise
           ┌─────────────────────────────┐
           │  5 . MNO                    │  ← Is 5 the qty? the line#?
           │  4 JkL Iop} .              │  ← Where does code end, qty begin?
           │  deF - 'tt                  │  ← What is 'tt? A qty? noise?
           └─────────────────────────────┘

  PROPOSED: Know exact cell boundaries → OCR only what matters
           ┌─────┬──────────┬───────────┐
           │  5  │  MNO     │    10     │  ← Code=MNO (letters), Qty=10 (digits)
           │  4  │  JKL     │     2     │  ← Code=JKL (letters), Qty=2 (digits)
           │  2  │  DEF     │     3     │  ← Code=DEF (letters), Qty=3 (digits)
           └─────┴──────────┴───────────┘
             ↑ pre-printed,    ↑ digit-only OCR
               scanner skips     = no confusion
```

---

## 7. Detailed Receipt Template (Print-Ready Specification)

### 7.1 Full Template Design

```
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║    ■                        RECEIPT                          ■    ║
║                                                                   ║
║    Date: ____/____/________        Receipt #: _______________    ║
║                                                                   ║
║  ┌──────┬─────────────────────┬──────────────┬────────────────┐  ║
║  │ S.No │    PRODUCT CODE     │   QUANTITY   │     UNIT       │  ║
║  │      │  (BLOCK CAPITALS)   │ (NUMBER ONLY)│                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  1   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  2   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  3   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  4   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  5   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  6   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  7   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  8   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │  9   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┼─────────────────────┼──────────────┼────────────────┤  ║
║  │      │                     │              │                │  ║
║  │ 10   │                     │              │                │  ║
║  │      │                     │              │                │  ║
║  ├──────┴─────────────────────┴──────────────┴────────────────┤  ║
║  │                                                            │  ║
║  │  Total Items:  [________]        Prepared By: ___________ │  ║
║  │                                                            │  ║
║  └────────────────────────────────────────────────────────────┘  ║
║                                                                   ║
║    ■                                                         ■    ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝

  ■ = Corner marker (solid black square, 8mm × 8mm)
```

### 7.2 Template Printing Specifications

| Property | Value |
|----------|-------|
| **Paper Size** | A5 (148 × 210 mm) or Half-Letter (140 × 216 mm) |
| **Paper Color** | White (minimum 90 brightness) |
| **Paper Weight** | 80 gsm or higher (prevents bleed-through) |
| **Border Line Width** | 0.5–0.8 pt (clearly visible but doesn't dominate) |
| **Border Color** | Black or Dark Gray (#333333) |
| **S.No Numbers** | Pre-printed in gray (#999999), 12pt font |
| **Column Headers** | Pre-printed in black, Bold, 10pt font |
| **Sub-labels** | "(BLOCK CAPITALS)" and "(NUMBER ONLY)" in gray 7pt |
| **Corner Markers** | Solid black squares, 8mm × 8mm, exactly at corners |
| **Cell Height** | 12–15mm (comfortable for handwriting) |
| **Cell Padding** | 2mm internal padding (text doesn't touch borders) |

### 7.3 Don'ts for Template Design

| Don't | Why |
|-------|-----|
| Don't use colored borders | Color borders may confuse grid detection |
| Don't use rounded corners on cells | Sharp corners are easier to detect |
| Don't use dotted/dashed lines | Solid lines are detected more reliably |
| Don't make cells too small (< 10mm) | Writers will overflow into adjacent cells |
| Don't add decorative elements | They create noise for the scanner |
| Don't use thin paper | Back-side text bleeds through and confuses OCR |

---

## 8. Camera/Phone Scanning Best Practices

### 8.1 Photography Guidelines (for Mobile Scanning)

```
  ╔════════════════════════════════════════════╗
  ║        HOW TO PHOTOGRAPH A RECEIPT         ║
  ╠════════════════════════════════════════════╣
  ║                                            ║
  ║  1. Place receipt on a FLAT, DARK surface  ║
  ║     (contrast helps edge detection)        ║
  ║                                            ║
  ║  2. Ensure EVEN LIGHTING                   ║
  ║     No shadows across the receipt          ║
  ║     Avoid flash (causes glare)             ║
  ║                                            ║
  ║  3. Hold phone DIRECTLY ABOVE              ║
  ║     (bird's eye view, not angled)          ║
  ║                                            ║
  ║  4. All 4 CORNER MARKERS must be visible   ║
  ║                                            ║
  ║  5. Keep receipt FLAT (no curling/folding) ║
  ║                                            ║
  ║  6. Minimum resolution: 8 megapixels       ║
  ║     (most modern phones exceed this)       ║
  ║                                            ║
  ╚════════════════════════════════════════════╝
```

### 8.2 Ideal vs Problem Photos

```
  ✅ IDEAL:                          ❌ PROBLEM:

  ┌─────────────────┐               ╱─────────────╲
  │                 │              ╱               ╲
  │  ■         ■    │             │  ■              │
  │  ┌─┬───┬──┐    │             │  ┌─┬───┬──      │  ← angled, markers cut off
  │  │ │   │  │    │             │  │ │   │         │
  │  ├─┼───┼──┤    │              ╲ ├─┼───┼        ╱
  │  │ │   │  │    │               ╲│ │   │      ╱
  │  ├─┼───┼──┤    │                ╲────────────╱
  │  │ │   │  │    │
  │  └─┴───┴──┘    │
  │  ■         ■    │  ← all 4 corners visible
  │                 │
  └─────────────────┘
  Flat, centered,                   Tilted, cropped,
  even lighting                     shadow on left side
```

---

## 9. Summary: The Complete Optimized Workflow

```
  Writer fills receipt    Phone scans receipt     App processes scan
  ─────────────────────   ──────────────────────  ─────────────────────────────

  ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────────┐
  │ Uses boxed grid │    │ Photograph with  │    │ 1. Detect 4 corner      │
  │ template        │ →  │ all 4 corners    │ →  │    markers (50ms)       │
  │                 │    │ visible          │    │                         │
  │ BLOCK CAPITALS  │    │                  │    │ 2. Perspective warp     │
  │ in CODE column  │    │ Even lighting    │    │    to rectangle (50ms)  │
  │                 │    │                  │    │                         │
  │ DIGITS ONLY     │    │ Flat surface     │    │ 3. Detect grid lines    │
  │ in QTY column   │    │                  │    │    & extract cells      │
  │                 │    │                  │    │    (200ms)              │
  │ Dark ballpoint  │    │                  │    │                         │
  │ pen             │    │                  │    │ 4. Skip empty cells     │
  │                 │    │                  │    │    (50ms)               │
  │ No QT suffix    │    │                  │    │                         │
  │                 │    │                  │    │ 5. OCR CODE cells       │
  └─────────────────┘    └──────────────────┘    │    (letters only)       │
                                                  │    (500ms)             │
                                                  │                         │
                                                  │ 6. OCR QTY cells        │
                                                  │    (digits only)        │
                                                  │    (300ms)             │
                                                  │                         │
                                                  │ 7. Map codes to         │
                                                  │    catalog (10ms)       │
                                                  │                         │
                                                  │ 8. Validate total       │
                                                  │    items count (5ms)    │
                                                  │                         │
                                                  │ TOTAL: ~1.2 seconds     │
                                                  │ vs current ~15 seconds  │
                                                  └─────────────────────────┘
```

---

## 10. Quick Reference: Before vs After

| Aspect | Current (Free-Form) | Proposed (Boxed Template) |
|--------|:------------------:|:------------------------:|
| Receipt format | Blank paper | Pre-printed grid |
| Line numbers | Handwritten (confuses scanner) | Pre-printed (scanner ignores) |
| QT suffix needed | Yes (`10QT`) | No (just `10`) |
| Code/Qty separation | Guessed by Y-grouping | Known by column position |
| OCR area | Full image (~1.3M pixels) | ~10 small cells (~50K pixels) |
| OCR passes | 2 (gray + color) | 1 (cells only) |
| Character confusion | High (letters ↔ digits) | Low (restricted charset per column) |
| Processing time | ~15 seconds | ~1–3 seconds |
| Code accuracy | 100% | 99–100% |
| Qty accuracy | 88% | 97–100% |
| Needs human review | ~70% of receipts | ~5% of receipts |

---

*Document prepared: February 22, 2026*
*Project: Handwritten Receipt Scanner — OCR Optimization*
