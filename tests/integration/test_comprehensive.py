"""
Comprehensive scanner test: tests ALL sample images directly through the pipeline.
No HTTP server needed — calls the OCR pipeline directly for speed.
Reports accuracy (items found, codes correct, quantities correct) and timing.
"""

import os
import sys
import time

# Setup path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

SAMPLE_DIR = os.path.join(BASE_DIR, "tests", "sample_inputs")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")

# ── Expected results ─────────────────────────────────────────────────────────
# Ground truth from manual inspection + OCR validation
EXPECTED = {
    # Gemini 1: AI-generated structured receipt (5 items, boxed template style)
    "Gemini_Generated_Image_ewg7o4ewg7o4ewg7.png": {
        "type": "structured",
        "items": [
            {"code": "ABC", "qty": 2},
            {"code": "DEF", "qty": 3},
            {"code": "GHI", "qty": 1},
            {"code": "JKL", "qty": 2},
            {"code": "MNO", "qty": 10},
        ]
    },
    # Gemini 2: AI-generated structured receipt (9 items, all products except XYZ)
    "Gemini_Generated_Image_lw7woslw7woslw7w.png": {
        "type": "structured",
        "items": [
            {"code": "ABC", "qty": 5},
            {"code": "DEF", "qty": 12},
            {"code": "GHI", "qty": 3},
            {"code": "JKL", "qty": 7},
            {"code": "MNO", "qty": 5},
            {"code": "PQR", "qty": 2},
            {"code": "STU", "qty": 1},
            {"code": "VWX", "qty": 15},
            {"code": "XYZ", "qty": 10},
            {"code": "RST", "qty": 6},
        ]
    },
    # Media (2): Handwritten receipt on ruled paper, 5 items
    # Known OCR limitations: JKL qty misread (2qt→Iop}→10), MNO qty from line number
    "Media (2).jpg": {
        "type": "handwritten",
        "items": [
            {"code": "ABC", "qty": 2},
            {"code": "DEF", "qty": 3},
            {"code": "GHI", "qty": 1},
            {"code": "JKL", "qty": 2},
            {"code": "MNO", "qty": 10},
        ]
    },
    # Media (3): Receipt with 3 items
    "Media (3).jpg": {
        "type": "structured",
        "items": [
            {"code": "MNO", "qty": 10},
            {"code": "GHI", "qty": 1},
            {"code": "JKL", "qty": 4},
        ]
    },
    # Media (4): Small handwritten receipt, 4 items
    "Media (4).jpg": {
        "type": "handwritten",
        "items": [
            {"code": "XYZ", "qty": 10},
            {"code": "ABC", "qty": 3},
            {"code": "VWX", "qty": 4},
            {"code": "STU", "qty": 2},
        ]
    },
    # Media (5): Handwritten receipt, 4 items
    "Media (5).jpg": {
        "type": "handwritten",
        "items": [
            {"code": "STU", "qty": 4},
            {"code": "XYZ", "qty": 14},
            {"code": "RST", "qty": 1},
            {"code": "VWX", "qty": 2},
        ]
    },
}


def test_image(filepath, filename, preprocessor, ocr_engine, parser):
    """Process a single image through the full pipeline and return results."""
    import cv2

    from app.config import IMAGE_MAX_DIMENSION, OCR_SMART_PASS_THRESHOLD

    result = {
        "filename": filename,
        "success": False,
        "items": {},
        "item_list": [],
        "unparsed": [],
        "timings": {},
        "receipt_type": "unknown",
        "ocr_passes": 0,
        "raw_ocr_texts": [],
    }

    total_start = time.time()

    # Step 1: Preprocess
    t0 = time.time()
    processed_image, preprocess_meta = preprocessor.preprocess(filepath)
    result["timings"]["preprocess_ms"] = int((time.time() - t0) * 1000)

    # Step 2: Detect structure
    t0 = time.time()
    is_structured = preprocessor.detect_grid_structure(processed_image)
    result["receipt_type"] = "structured" if is_structured else "handwritten"
    result["timings"]["grid_detect_ms"] = int((time.time() - t0) * 1000)

    # Step 3: OCR
    t0 = time.time()
    cropped_gray = preprocessor.crop_to_content(processed_image)

    if is_structured:
        gray_results = ocr_engine.extract_text_turbo(cropped_gray)
        result["ocr_passes"] = 1
    else:
        gray_results = ocr_engine.extract_text_fast(cropped_gray)
        result["ocr_passes"] = 1

    # Quick item count
    catalog = parser.product_catalog
    found_codes = set()
    for r in gray_results:
        text = r.get("text", "").upper().strip()
        for token in text.split():
            clean = ''.join(c for c in token if c.isalpha())
            if 2 <= len(clean) <= 6 and clean in catalog:
                found_codes.add(clean)
    quick_count = len(found_codes)

    alt_results = []
    if is_structured:
        ocr_results = gray_results
    elif quick_count < OCR_SMART_PASS_THRESHOLD:
        # Color pass needed
        original_color = cv2.imread(filepath)
        if original_color is not None:
            h, w = original_color.shape[:2]
            max_dim = IMAGE_MAX_DIMENSION
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                original_color = cv2.resize(original_color, None, fx=scale, fy=scale)
            color_results = ocr_engine.extract_text(original_color)
            result["ocr_passes"] = 2
            if len(color_results) > len(gray_results):
                alt_results = gray_results
                ocr_results = color_results
            else:
                alt_results = color_results
                ocr_results = gray_results
        else:
            ocr_results = gray_results
    else:
        ocr_results = gray_results

    result["timings"]["ocr_ms"] = int((time.time() - t0) * 1000)
    result["raw_ocr_texts"] = [r.get("text", "") for r in ocr_results]

    # Step 4: Parse
    t0 = time.time()
    receipt_data = parser.parse(ocr_results)

    # Union merge alt results (with cap: don't override when alt > 2.5× primary)
    if alt_results:
        alt_data = parser.parse(alt_results)
        if alt_data.get("items"):
            primary_items = {item["code"]: item for item in receipt_data.get("items", [])}
            for alt_item in alt_data["items"]:
                code = alt_item["code"]
                if code in primary_items:
                    pri_qty = primary_items[code]["quantity"]
                    alt_qty = alt_item["quantity"]
                    if 1 <= alt_qty <= 99 and alt_qty > pri_qty:
                        if pri_qty <= 1.0:
                            # Primary missed qty — use alt
                            primary_items[code]["quantity"] = alt_qty
                        elif alt_qty <= pri_qty * 2.5:
                            # Modestly higher — could be more accurate
                            primary_items[code]["quantity"] = alt_qty
                        # else: alt is wildly higher (>2.5×) — skip
                else:
                    receipt_data["items"].append(alt_item)
                    receipt_data["total_items"] = len(receipt_data["items"])

    result["timings"]["parse_ms"] = int((time.time() - t0) * 1000)
    result["timings"]["total_ms"] = int((time.time() - total_start) * 1000)

    # Collect results
    for item in receipt_data.get("items", []):
        code = item.get("code", "?")
        qty = item.get("quantity", 0)
        result["items"][code] = qty
        result["item_list"].append({
            "code": code,
            "qty": qty,
            "product": item.get("product", "?"),
            "confidence": item.get("confidence", 0),
            "match_type": item.get("match_type", "?"),
            "needs_review": item.get("needs_review", False),
        })

    result["unparsed"] = receipt_data.get("unparsed_lines", [])
    result["success"] = len(result["items"]) > 0
    return result


def print_result(result, expected_items=None):
    """Print detailed results for one image."""
    filename = result["filename"]
    timings = result["timings"]

    print(f"\n{'='*75}")
    print(f"  {filename}")
    print(f"  Type: {result['receipt_type']} | OCR passes: {result['ocr_passes']}")
    print(f"{'='*75}")

    print("  Timing:")
    print(f"    Preprocess : {timings.get('preprocess_ms', 0):>6}ms")
    print(f"    Grid detect: {timings.get('grid_detect_ms', 0):>6}ms")
    print(f"    OCR        : {timings.get('ocr_ms', 0):>6}ms")
    print(f"    Parse      : {timings.get('parse_ms', 0):>6}ms")
    print(f"    TOTAL      : {timings.get('total_ms', 0):>6}ms ({timings.get('total_ms', 0)/1000:.1f}s)")

    print(f"\n  Items found ({len(result['item_list'])}):")
    print(f"  {'Code':<8} {'Qty':>6}  {'Product':<30} {'Match':<12} {'Conf':>6}")
    print(f"  {'---':<8} {'---':>6}  {'---':<30} {'---':<12} {'---':>6}")
    for item in result["item_list"]:
        review = " !" if item["needs_review"] else ""
        print(f"  {item['code']:<8} {item['qty']:>6.1f}  {item['product']:<30} {item['match_type']:<12} {item['confidence']:>5.1%}{review}")

    if result["unparsed"]:
        print(f"\n  Unparsed lines ({len(result['unparsed'])}):")
        for u in result["unparsed"][:10]:
            print(f"    - {u.get('text', '?')!r}")

    # Accuracy check
    codes_correct = 0
    qty_correct = 0
    total_expected = 0
    passed = True

    if expected_items:
        total_expected = len(expected_items)
        print("\n  Accuracy Check:")
        for exp in expected_items:
            exp_code = exp["code"]
            exp_qty = exp["qty"]
            actual_qty = result["items"].get(exp_code)

            if actual_qty is not None:
                codes_correct += 1
                code_status = "OK"
                if abs(actual_qty - exp_qty) < 0.01:
                    qty_correct += 1
                    qty_status = "OK"
                else:
                    qty_status = f"WRONG (got {actual_qty}, want {exp_qty})"
                    passed = False
            else:
                code_status = "MISSING"
                qty_status = "--"
                passed = False

            status_icon = "OK" if code_status == "OK" and qty_status == "OK" else "XX"
            print(f"    [{status_icon}] {exp_code}: code={code_status}, qty={qty_status}")

        # Extra items
        expected_codes = {e["code"] for e in expected_items}
        extra = set(result["items"].keys()) - expected_codes
        if extra:
            print(f"    [!!] Extra items (false positives): {extra}")
            passed = False

        code_pct = codes_correct / total_expected * 100 if total_expected else 0
        qty_pct = qty_correct / total_expected * 100 if total_expected else 0
        overall = "PASS" if passed else "FAIL"
        print(f"\n    Code accuracy: {codes_correct}/{total_expected} ({code_pct:.0f}%)")
        print(f"    Qty  accuracy: {qty_correct}/{total_expected} ({qty_pct:.0f}%)")
        print(f"    Result       : {overall}")
    else:
        print("\n  [Discovery mode - no expected values defined]")

    return {
        "passed": passed,
        "codes_correct": codes_correct,
        "qty_correct": qty_correct,
        "total_expected": total_expected,
        "total_found": len(result["items"]),
        "time_ms": timings.get("total_ms", 0),
        "receipt_type": result["receipt_type"],
    }


def main():
    import logging
    logging.basicConfig(level=logging.WARNING)  # Suppress debug noise

    print("=" * 75)
    print("  COMPREHENSIVE SCANNER TEST")
    print("  Testing all sample inputs through the full OCR pipeline")
    print("=" * 75)

    # Initialize pipeline components once (includes EasyOCR model load)
    print("\nInitializing OCR engine (one-time model load)...")
    init_start = time.time()

    from app.ocr.engine import get_ocr_engine
    from app.ocr.parser import ReceiptParser
    from app.ocr.preprocessor import ImagePreprocessor
    from app.services.product_service import product_service

    preprocessor = ImagePreprocessor()
    ocr_engine = get_ocr_engine()
    catalog = product_service.get_product_code_map()
    parser = ReceiptParser(catalog)

    init_ms = int((time.time() - init_start) * 1000)
    print(f"Engine initialized in {init_ms}ms ({init_ms/1000:.1f}s)")
    print(f"Catalog: {list(catalog.keys())}")

    # Discover all sample images
    images = sorted([
        f for f in os.listdir(SAMPLE_DIR)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
    ])
    print(f"\nFound {len(images)} sample images in {SAMPLE_DIR}:")
    for img in images:
        fpath = os.path.join(SAMPLE_DIR, img)
        size_kb = os.path.getsize(fpath) / 1024
        tag = " (expected)" if img in EXPECTED else " (discovery)"
        print(f"  - {img} ({size_kb:.0f} KB){tag}")

    # Also check for boxed template in uploads
    boxed_template = None
    for f in os.listdir(UPLOADS_DIR):
        if f == "upload_20260222_213048.png":
            boxed_template = os.path.join(UPLOADS_DIR, f)
            print(f"\n  + Boxed template: {f} (from uploads/)")

    # ── Run tests ──
    all_results = {}
    summary_rows = []

    for img_name in images:
        img_path = os.path.join(SAMPLE_DIR, img_name)
        expected_items = EXPECTED.get(img_name, {}).get("items")

        result = test_image(img_path, img_name, preprocessor, ocr_engine, parser)
        analysis = print_result(result, expected_items)
        all_results[img_name] = {**result, **analysis}
        summary_rows.append((img_name, analysis))

    # Test boxed template if found
    if boxed_template:
        # Actual values on the receipt (verified via raw OCR dump)
        boxed_expected = [
            {"code": "ABC", "qty": 2},
            {"code": "DEF", "qty": 3},
            {"code": "GHI", "qty": 1},
            {"code": "JKL", "qty": 2},
            {"code": "MNO", "qty": 10},
        ]
        result = test_image(boxed_template, "Boxed Template (5-item)", preprocessor, ocr_engine, parser)
        analysis = print_result(result, boxed_expected)
        all_results["boxed_template"] = {**result, **analysis}
        summary_rows.append(("Boxed Template", analysis))

    # ── Grand Summary ──
    print(f"\n{'='*75}")
    print("  GRAND SUMMARY")
    print(f"{'='*75}")

    total_codes_correct = 0
    total_codes_expected = 0
    total_qty_correct = 0
    total_time = 0

    print(f"\n  {'Image':<35} {'Result':<10} {'Items':>6} {'Code%':>7} {'Qty%':>7} {'Time':>8} {'Type':<12}")
    print(f"  {'---':<35} {'---':<10} {'---':>6} {'---':>7} {'---':>7} {'---':>8} {'---':<12}")

    for img_name, analysis in summary_rows:
        if analysis["total_expected"] > 0:
            status = "PASS" if analysis["passed"] else "FAIL"
            code_pct = f"{analysis['codes_correct']}/{analysis['total_expected']}"
            qty_pct = f"{analysis['qty_correct']}/{analysis['total_expected']}"
            total_codes_correct += analysis["codes_correct"]
            total_codes_expected += analysis["total_expected"]
            total_qty_correct += analysis["qty_correct"]
        else:
            status = "DISC"
            code_pct = f"{analysis['total_found']}/??"
            qty_pct = "??"

        total_time += analysis["time_ms"]
        print(f"  {img_name:<35} {status:<10} {analysis['total_found']:>6} {code_pct:>7} {qty_pct:>7} {analysis['time_ms']:>7}ms {analysis['receipt_type']:<12}")

    print(f"\n  {'TOTALS':<35} {'':>10} {'':>6} {total_codes_correct}/{total_codes_expected}   {total_qty_correct}/{total_codes_expected}   {total_time:>7}ms")

    if total_codes_expected > 0:
        overall_code_pct = total_codes_correct / total_codes_expected * 100
        overall_qty_pct = total_qty_correct / total_codes_expected * 100
        print(f"\n  Overall Code Accuracy: {overall_code_pct:.0f}%")
        print(f"  Overall Qty  Accuracy: {overall_qty_pct:.0f}%")
        print(f"  Average Time/Image   : {total_time // len(summary_rows)}ms")

        all_passed = all(
            a["passed"] for _, a in summary_rows if a["total_expected"] > 0
        )
        print(f"\n  {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    else:
        print("\n  No expected values to compare against.")

    # Return exit code
    all_passed = all(
        a["passed"] for _, a in summary_rows if a["total_expected"] > 0
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
