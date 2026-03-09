# AI Prompts to Generate Printable Receipt Templates for Testing

Use these prompts with AI image generators (ChatGPT/DALL-E, Midjourney, Adobe Firefly,
Canva AI, or any text-to-image tool) to generate receipt images you can **print and scan**
to test the OCR scanner.

---

## Prompt 1 — Blank Template (Print and Fill by Hand)

> **Copy-paste this into ChatGPT / DALL-E / Midjourney:**

```
Create a clean, high-resolution image of a printed receipt form on white paper.
The receipt has the title "RECEIPT" centered at the top in bold black text.
Below the title there is a line for "Date: ___/___/______" on the left and
"Receipt #: ________" on the right.

Below that is a table/grid with clearly drawn solid black border lines forming
rows and columns. The table has 4 columns with these headers in bold:
"S.No", "PRODUCT CODE", "QUANTITY", "UNIT".

Under "PRODUCT CODE" in smaller gray text it says "(BLOCK CAPITALS)".
Under "QUANTITY" in smaller gray text it says "(NUMBER ONLY)".

The table has 10 numbered rows. The S.No column has pre-printed numbers
1 through 10 in gray text. The other 3 columns are empty white boxes
ready to be filled in by hand.

Below the table there is a row that says "Total Items: [____]" and
"Prepared By: ____________".

There are 4 small solid black squares (8mm size) at the four corners of
the receipt acting as alignment markers.

The overall look is a clean professional printed form on white paper,
like a pre-printed receipt pad. No handwriting. Straight lines. Black borders.
White background. High contrast. Top-down flat view, no perspective distortion.
Resolution suitable for A5 paper printing (148mm x 210mm).
```

---

## Prompt 2 — Filled Receipt with 5 Items (Handwritten Look)

> **Use this to generate a realistic test image WITH handwritten entries:**

```
Create a photo-realistic image of a handwritten receipt on a pre-printed form.
The form is on white paper with printed black grid lines forming a table.

The table has 4 columns: "S.No", "PRODUCT CODE", "QUANTITY", "UNIT".
The S.No numbers (1-10) are pre-printed in gray.

The following items are handwritten in BLOCK CAPITAL letters using a dark
blue or black ballpoint pen. The handwriting should look natural but legible:

Row 1: Product Code = "ABC", Quantity = "2"
Row 2: Product Code = "DEF", Quantity = "3"
Row 3: Product Code = "GHI", Quantity = "1"
Row 4: Product Code = "JKL", Quantity = "2"
Row 5: Product Code = "MNO", Quantity = "10"

Rows 6 through 10 are left empty (blank white cells).

At the bottom, "Total Items: 5" is handwritten.

The image should look like a top-down photograph of this receipt lying flat
on a dark desk surface. Good even lighting, no shadows, no glare, no blur.
All 4 corner markers (small black squares) are visible.
The receipt takes up most of the frame.
```

---

## Prompt 3 — Filled Receipt with 4 Items (Different Products)

```
Create a photo-realistic top-down photograph of a handwritten receipt on a
pre-printed grid form lying flat on a dark surface.

The form has a title "RECEIPT" at the top, a date field, and a table with
4 columns: "S.No" (pre-printed 1-10 in gray), "PRODUCT CODE",
"QUANTITY", "UNIT".

Handwritten in block capitals with a black ballpoint pen:

Row 1: Product Code = "XYZ", Quantity = "10"
Row 2: Product Code = "ABC", Quantity = "3"
Row 3: Product Code = "VWX", Quantity = "4"
Row 4: Product Code = "STU", Quantity = "2"

Rows 5-10 are blank. Total Items at bottom says "4".

Natural handwriting, legible but not perfect. Even lighting, sharp focus,
no perspective distortion. High resolution suitable for OCR scanning.
Four small black square corner markers visible at receipt corners.
```

---

## Prompt 4 — Filled Receipt with Duplicate Product Code

```
Create a realistic photograph of a handwritten receipt on a pre-printed
grid template, viewed from directly above on a dark desk.

The table has columns: "S.No" (pre-printed 1-10), "PRODUCT CODE",
"QUANTITY", "UNIT". All borders are solid black lines.

Handwritten entries in BLOCK CAPITALS with black pen:

Row 1: Product Code = "STU", Quantity = "4"
Row 2: Product Code = "XYZ", Quantity = "10"
Row 3: Product Code = "RST", Quantity = "1"
Row 4: Product Code = "VWX", Quantity = "2"
Row 5: Product Code = "XYZ", Quantity = "4"

Rows 6-10 are empty. Total Items says "5".

Note: XYZ appears twice (rows 2 and 5) - this is intentional.

Good lighting, no shadows, sharp, high resolution. Corner alignment
markers visible. The handwriting looks natural, like a real person wrote it
with a ballpoint pen - slightly imperfect but clearly readable.
```

---

## Prompt 5 — Stress Test (All 10 Rows Filled)

```
Generate a photo-realistic image of a fully filled handwritten receipt on
a pre-printed grid form. Top-down view, flat on dark surface, even lighting.

Table columns: "S.No" (pre-printed 1-10), "PRODUCT CODE", "QUANTITY", "UNIT".

All 10 rows are filled with handwritten BLOCK CAPITALS in black pen:

Row 1:  Code = "ABC", Qty = "5"
Row 2:  Code = "DEF", Qty = "12"
Row 3:  Code = "GHI", Qty = "3"
Row 4:  Code = "JKL", Qty = "7"
Row 5:  Code = "MNO", Qty = "1"
Row 6:  Code = "PQR", Qty = "20"
Row 7:  Code = "STU", Qty = "8"
Row 8:  Code = "VWX", Qty = "15"
Row 9:  Code = "XYZ", Qty = "2"
Row 10: Code = "RST", Qty = "6"

Total Items at bottom: "10". The handwriting varies slightly per row
(natural variation in pen pressure and letter size). All text stays
within cell boundaries. Sharp focus, high resolution, no blur or glare.
Four black square corner markers at receipt corners.
```

---

## Prompt 6 — Messy/Challenging Handwriting (Hard Test Case)

```
Create a realistic photograph of a handwritten receipt that would be
challenging for OCR. The receipt is on a pre-printed grid form with
columns: "S.No" (pre-printed), "PRODUCT CODE", "QUANTITY", "UNIT".

Handwritten with a slightly thick pen, the writing is a bit rushed:

Row 1: Code = "GHI", Qty = "9"   (the G looks slightly like a 6)
Row 2: Code = "MNO", Qty = "11"  (the 11 is written close together)
Row 3: Code = "STU", Qty = "3"   (the S could be mistaken for 5)
Row 4: Code = "PQR", Qty = "14"  (the P has an open loop)

The handwriting is legible but imperfect - like someone writing quickly.
Some letters are slightly slanted. Pen strokes have variable thickness.
Still block capitals but not perfectly neat.

Top-down photo, slightly warm lighting, receipt on a wooden desk surface.
High resolution. Corner markers visible.
```

---

## Tips for Best Results

### Which AI Tool to Use

| Tool | Best For | Notes |
|------|----------|-------|
| **ChatGPT (DALL-E 3)** | Most realistic handwriting | Best at following detailed layout instructions |
| **Midjourney v6** | Photorealistic paper texture | Add `--ar 2:3 --v 6` for portrait receipt shape |
| **Adobe Firefly** | Clean structured forms | Good at grid/table layouts |
| **Canva AI** | Quick iterations | Easy to edit and re-generate |
| **Stable Diffusion** | Local generation | Use ControlNet for precise grid layout |

### Important Add-On Phrases

Add these to any prompt for better results:

- `"top-down flat lay photography"` — prevents perspective distortion
- `"8K resolution, sharp focus"` — ensures OCR-quality detail
- `"no text artifacts, no misspellings in printed text"` — prevents AI hallucinated characters
- `"the handwritten text must be exactly as specified, no extra characters"` — prevents random text
- `"solid black grid lines, white cell backgrounds"` — ensures grid detection will work

### After Generating

1. **Check the image** — verify product codes and quantities match what you specified
2. **Print at actual size** on white paper (A5 or half-letter)
3. **Photograph it** with your phone (or scan with a flatbed scanner)
4. **Upload** to the Receipt Scanner app for testing
5. **Compare** OCR results against the known values

### Expected Test Results

| Prompt | Expected Items | Expected Codes | Expected Quantities |
|--------|:-:|---|---|
| Prompt 2 | 5 | ABC, DEF, GHI, JKL, MNO | 2, 3, 1, 2, 10 |
| Prompt 3 | 4 | XYZ, ABC, VWX, STU | 10, 3, 4, 2 |
| Prompt 4 | 5 (4 unique, XYZ×2) | STU, XYZ, RST, VWX | 4, 14, 1, 2 |
| Prompt 5 | 10 | All 10 codes | 5,12,3,7,1,20,8,15,2,6 |
| Prompt 6 | 4 | GHI, MNO, STU, PQR | 9, 11, 3, 14 |

---

*Use these prompts to generate as many test receipts as you need.
Each prompt maps to the exact product codes (ABC, DEF, GHI, JKL, MNO, PQR, STU, VWX, XYZ, RST)
in your scanner's catalog, so results can be verified automatically.*
