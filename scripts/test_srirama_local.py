"""
Test script: Scan Sri Rama Paints receipts using LOCAL OCR only.
Bypasses Azure to avoid network delays. Writes results to a text file.
"""
import os, sys, time

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"
OUTFILE = FIXTURES / "srirama_scan_results.txt"

IMAGES = [
    ("srirama_paints_filled.jpg", "Filled table receipt"),
    ("srirama_paints_handwritten.jpg", "Handwritten detailed receipt"),
]

lines = []
def log(msg=""):
    lines.append(msg)
    print(msg)

def main():
    log("=" * 70)
    log("  SRI RAMA PAINTS - OCR SCAN TEST (LOCAL ENGINE)")
    log("=" * 70)

    for fname, desc in IMAGES:
        p = FIXTURES / fname
        log(f"  [OK] {fname} ({p.stat().st_size // 1024} KB) - {desc}")
    log()

    # Init OCR
    log("Loading OCR engine...")
    t0 = time.time()

    from app.ocr.preprocessor import ImagePreprocessor
    from app.ocr.engine import get_ocr_engine
    from app.ocr.parser import ReceiptParser
    from app.services.product_service import product_service

    preprocessor = ImagePreprocessor()
    ocr_engine = get_ocr_engine()  # Direct local EasyOCR engine
    catalog = product_service.get_product_code_map()
    parser = ReceiptParser(catalog)

    log(f"OCR engine loaded in {time.time() - t0:.1f}s")
    log()

    for fname, desc in IMAGES:
        fpath = FIXTURES / fname
        log("-" * 70)
        log(f"SCANNING: {fname}")
        log(f"  ({desc})")
        log("-" * 70)

        # Step 1: Preprocess
        t1 = time.time()
        try:
            processed, metadata = preprocessor.preprocess(str(fpath))
            preprocess_ms = (time.time() - t1) * 1000
            log(f"\n  [PREPROCESS] {preprocess_ms:.0f}ms")
            if metadata:
                q = metadata.get("quality_assessment", {})
                if q:
                    log(f"    Sharpness : {q.get('sharpness', 'N/A')}")
                    log(f"    Brightness: {q.get('brightness', 'N/A')}")
                    log(f"    Contrast  : {q.get('contrast', 'N/A')}")
                log(f"    Doc scanner: {'Yes' if metadata.get('document_scanner_applied') else 'No'}")
                log(f"    Deskew     : {'Yes' if metadata.get('deskew_applied') else 'No'}")
        except Exception as e:
            log(f"  [ERROR] Preprocess failed: {e}")
            import traceback; log(traceback.format_exc())
            continue

        # Step 2: Local OCR (FULL pass on grayscale)
        t2 = time.time()
        try:
            log("\n  [OCR] Running EasyOCR full pass (grayscale)...")
            detections_gray = ocr_engine.extract_text(processed)
            gray_ms = (time.time() - t2) * 1000
            log(f"    Gray pass: {gray_ms:.0f}ms, {len(detections_gray)} detections")

            # Also try color pass if available
            color_img = metadata.get("_color_img") if metadata else None
            detections_color = []
            if color_img is not None:
                t2b = time.time()
                log("  [OCR] Running EasyOCR fast pass (color)...")
                detections_color = ocr_engine.extract_text_fast(color_img)
                color_ms = (time.time() - t2b) * 1000
                log(f"    Color pass: {color_ms:.0f}ms, {len(detections_color)} detections")

            # Merge: use the pass with more detections
            detections = detections_gray if len(detections_gray) >= len(detections_color) else detections_color
            log(f"    Best pass: {len(detections)} detections")

            # Print all raw OCR text
            log(f"\n  [RAW TEXT] ({len(detections)} regions):")
            for i, det in enumerate(detections):
                text = det[1] if isinstance(det, (list, tuple)) and len(det) > 1 else str(det)
                conf = det[2] if isinstance(det, (list, tuple)) and len(det) > 2 else "?"
                if isinstance(conf, float):
                    conf = f"{conf:.2f}"
                log(f"    [{i+1:3d}] (conf={conf}) \"{text}\"")

        except Exception as e:
            log(f"  [ERROR] OCR failed: {e}")
            import traceback; log(traceback.format_exc())
            continue

        # Step 3: Parse
        t3 = time.time()
        try:
            parsed = parser.parse(detections)
            parse_ms = (time.time() - t3) * 1000
            log(f"\n  [PARSE] {parse_ms:.0f}ms")
            log(f"    Items found : {len(parsed.get('items', []))}")
            log(f"    Bill total  : {parsed.get('bill_total', 0)}")
            log(f"    Store name  : {parsed.get('store_name', 'N/A')}")
            log(f"    Receipt date: {parsed.get('receipt_date', 'N/A')}")

            items = parsed.get("items", [])
            if items:
                log(f"\n  [PARSED ITEMS]")
                log(f"    {'#':>3} {'Code':<12} {'Name':<35} {'Qty':>5} {'Price':>10}")
                log(f"    {'---':>3} {'------------':<12} {'-----------------------------------':<35} {'-----':>5} {'----------':>10}")
                for i, item in enumerate(items):
                    code = item.get("product_code", "?")
                    name = item.get("product_name", "?")
                    qty = item.get("quantity", "?")
                    price = item.get("line_total", item.get("unit_price", "?"))
                    log(f"    {i+1:3d} {code:<12} {name:<35} {str(qty):>5} {str(price):>10}")

        except Exception as e:
            log(f"  [ERROR] Parse failed: {e}")
            import traceback; log(traceback.format_exc())

        total_ms = (time.time() - t1) * 1000
        log(f"\n  [TOTAL TIME] {total_ms:.0f}ms")
        log()

    # Ground truth
    log("=" * 70)
    log("  GROUND TRUTH (from manual reading)")
    log("=" * 70)
    log("  Filled receipt: 13-15 line items, Total = Rs.47,422")
    log("  Key items: Royale L-143, PEP L-143, PEP White, Apex Ext 0684,")
    log("  Antek Set, L Patti blades, A.Ext Roller, Int Roller, Paper, Cloth,")
    log("  BSO Tape, 950 Roller")
    log()

    # Save results
    with open(OUTFILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"Results saved to: {OUTFILE}")


if __name__ == "__main__":
    main()
