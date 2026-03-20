"""
Test script: Scan Sri Rama Paints receipt images directly through the OCR pipeline.
Bypasses the HTTP server to test OCR accuracy on tabular paint receipts.

Usage:
    python scripts/test_srirama_scan.py
"""

import json
import os
import sys
import time

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"

# Images to test (only the ones with actual data)
TEST_IMAGES = [
    ("srirama_paints_filled.jpg", "Filled table receipt (13 handwritten line items)"),
    ("srirama_paints_handwritten.jpg", "Handwritten detailed receipt"),
]


def main():
    print("=" * 70)
    print("  🔍 Sri Rama Paints Receipt — OCR Test")
    print("=" * 70)

    # Check images exist
    for fname, desc in TEST_IMAGES:
        fpath = FIXTURES_DIR / fname
        if not fpath.exists():
            print(f"  ❌ Missing: {fpath}")
            return
        size_kb = fpath.stat().st_size / 1024
        print(f"  ✅ {fname} ({size_kb:.0f} KB) — {desc}")
    print()

    # Initialize OCR components
    print("⏳ Loading OCR engine (this takes ~20-30s on first run)...")
    t0 = time.time()

    from app.ocr.preprocessor import ImagePreprocessor
    from app.ocr.hybrid_engine import get_hybrid_engine
    from app.ocr.parser import ReceiptParser
    from app.services.product_service import product_service

    preprocessor = ImagePreprocessor()
    hybrid_engine = get_hybrid_engine()

    # Load product catalog for parser
    catalog = product_service.get_product_code_map()
    parser = ReceiptParser(catalog)

    print(f"✅ OCR engine loaded in {time.time() - t0:.1f}s\n")

    # Process each image
    for fname, desc in TEST_IMAGES:
        fpath = FIXTURES_DIR / fname
        print("─" * 70)
        print(f"📄 Scanning: {fname}")
        print(f"   Description: {desc}")
        print("─" * 70)

        # Step 1: Preprocess
        t1 = time.time()
        try:
            processed, metadata = preprocessor.preprocess(str(fpath))
            preprocess_ms = (time.time() - t1) * 1000
            print(f"\n  📐 Preprocessing: {preprocess_ms:.0f}ms")
            if metadata:
                quality = metadata.get("quality_assessment", {})
                if quality:
                    print(f"     Sharpness: {quality.get('sharpness', 'N/A')}")
                    print(f"     Brightness: {quality.get('brightness', 'N/A')}")
                    print(f"     Contrast: {quality.get('contrast', 'N/A')}")
                doc_scan = metadata.get("document_scanner_applied", False)
                print(f"     Doc scanner: {'Yes' if doc_scan else 'No'}")
                deskew = metadata.get("deskew_applied", False)
                print(f"     Deskew: {'Yes' if deskew else 'No'}")

                # Get color image for dual-pass
                color_img = metadata.get("_color_img", None)
        except Exception as e:
            print(f"  ❌ Preprocessing failed: {e}")
            import traceback
            traceback.print_exc()
            continue

        # Step 2: OCR
        t2 = time.time()
        try:
            color_img_for_ocr = metadata.get("_color_img") if metadata else None
            quality_info = metadata.get("quality_assessment") if metadata else None
            is_structured = metadata.get("is_structured", False) if metadata else False
            ocr_result = hybrid_engine.process_image(
                image_path=str(fpath),
                processed_image=processed,
                is_structured=is_structured,
                original_color=color_img_for_ocr,
                quality_info=quality_info,
            )
            ocr_ms = (time.time() - t2) * 1000
            print(f"\n  OCR: {ocr_ms:.0f}ms")
            print(f"     Engine used: {ocr_result.get('engine_used', 'unknown')}")
            print(f"     Confidence: {ocr_result.get('confidence_avg', 0):.2f}")

            # Print raw detections
            detections = ocr_result.get("ocr_detections", [])
            print(f"     Detections: {len(detections)}")

            if detections:
                print(f"\n  📝 Raw OCR Detections ({len(detections)} text regions):")
                for i, det in enumerate(detections):
                    text = det[1] if isinstance(det, (list, tuple)) and len(det) > 1 else str(det)
                    conf = det[2] if isinstance(det, (list, tuple)) and len(det) > 2 else "?"
                    if isinstance(conf, float):
                        conf = f"{conf:.2f}"
                    print(f"     [{i+1:2d}] ({conf}) {text}")

            # Print Azure structured data if available
            azure_data = ocr_result.get("azure_structured")
            if azure_data:
                azure_items = azure_data.get("items", [])
                print(f"\n  Azure Structured Items ({len(azure_items)}):")
                for i, item in enumerate(azure_items):
                    name = item.get("description", item.get("name", "?"))
                    qty = item.get("quantity", "?")
                    price = item.get("total_price", item.get("price", "?"))
                    print(f"     [{i+1:2d}] {name} x {qty} = {price}")

        except Exception as e:
            print(f"  ❌ OCR failed: {e}")
            import traceback
            traceback.print_exc()
            continue

        # Step 3: Parse
        t3 = time.time()
        try:
            parsed = parser.parse(detections)
            parse_ms = (time.time() - t3) * 1000
            print(f"\n  📋 Parsing: {parse_ms:.0f}ms")
            print(f"     Items found: {len(parsed.get('items', []))}")
            print(f"     Bill total: {parsed.get('bill_total', 0)}")
            print(f"     Store name: {parsed.get('store_name', 'N/A')}")
            print(f"     Receipt date: {parsed.get('receipt_date', 'N/A')}")

            items = parsed.get("items", [])
            if items:
                print(f"\n  🛒 Parsed Items:")
                print(f"     {'#':>3} {'Code':<12} {'Name':<30} {'Qty':>5} {'Price':>10}")
                print(f"     {'─'*3} {'─'*12} {'─'*30} {'─'*5} {'─'*10}")
                for i, item in enumerate(items):
                    code = item.get("product_code", "?")
                    name = item.get("product_name", "?")
                    qty = item.get("quantity", "?")
                    price = item.get("line_total", item.get("unit_price", "?"))
                    print(f"     {i+1:3d} {code:<12} {name:<30} {qty:>5} {price:>10}")

        except Exception as e:
            print(f"  ❌ Parsing failed: {e}")
            import traceback
            traceback.print_exc()

        total_ms = (time.time() - t1) * 1000
        print(f"\n  ⏱️  Total time: {total_ms:.0f}ms")
        print()

    # Ground truth comparison
    print("=" * 70)
    print("  📊 GROUND TRUTH (from image)")
    print("=" * 70)
    print("""
  Sri Rama Paints — Filled Receipt (13 line items):
  ─────────────────────────────────────────────────
   # │ Qty   │ Product              │ Shade Code │ Unit Rate │ Total
  ───┼───────┼──────────────────────┼────────────┼───────────┼───────
   1 │ 20L   │ Royale (L-143)       │ L143       │ 10450     │ 10450
   2 │ 20L   │ PEP (L-143)          │ L143       │ 5750      │ 5750
   3 │ 1×20L │ Royale               │ L143       │ 5670      │ 5670
   4 │1.1×20L│ PEP                  │ L143       │ 5750      │ 5750
   5 │ 1×20L │ PEP                  │ White      │ 5670      │ 5670
   6 │ 4×20L │ Apex Ext Emulsion    │ 0684       │ 5650      │ 22720
   7 │ 4 pcs │ Antek Set            │            │ 130       │ 520
   8 │ 8 pcs │ 8" L Patti (blade)   │            │ 25        │ 200
   9 │ 4 pcs │ 4" L Patti           │            │ 10        │ 40
  10 │ 2 pcs │ A. Ext Roller        │            │ 200       │ 400
  11 │ 2 pcs │ A. Int Roller Brush  │            │ 200       │ 400
  12 │ 50 pk │ 120# Paper           │            │ 9         │ 450
  13 │ 2 pcs │ Cloth                │            │ 45        │ 90
  14*│ 6 pcs │ A BSO Tape           │            │ 25        │ 150
  15*│ 2 pcs │ 950# Roller          │            │ 250       │ 500
  ─────────────────────────────────────────────
  Grand Total: ₹47,422
    """)


if __name__ == "__main__":
    main()
