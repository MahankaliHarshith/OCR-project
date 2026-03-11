"""
End-to-end OCR accuracy test across all 5 test receipt images.

Runs each image through the full pipeline (preprocess → OCR → parse)
and reports code detection accuracy and quantity accuracy.
"""

import sys
import time
from pathlib import Path

# ── Expected ground truth for each receipt image ──
GROUND_TRUTH = {
    "receipt_neat.jpg": {
        "TEW1": 3, "TEW4": 2, "PEPW10": 5, "PEPW20": 1,
    },
    "receipt_messy.jpg": {
        "TEW10": 2, "TEW20": 4, "PEPW1": 6, "PEPW4": 3,
    },
    "receipt_faded.jpg": {
        "TEW1": 1, "TEW10": 3, "PEPW1": 2, "PEPW10": 4,
    },
    "receipt_dense.jpg": {
        "TEW1": 2, "TEW4": 5, "TEW10": 1, "TEW20": 3,
        "PEPW1": 4, "PEPW4": 2, "PEPW10": 6, "PEPW20": 1,
    },
    "receipt_dark_ink.jpg": {
        "PEPW20": 3, "TEW4": 7, "PEPW10": 2, "TEW1": 5, "PEPW4": 1,
    },
}

IMAGE_DIR = Path("test_images")


def run_tests():
    # Import OCR pipeline components
    from app.services.receipt_service import receipt_service

    print("=" * 65)
    print("  OCR ACCURACY TEST - Post-Audit Optimization")
    print("=" * 65)

    total_codes_expected = 0
    total_codes_detected = 0
    total_qty_correct = 0
    total_qty_expected = 0
    results = []

    for img_name, expected_items in GROUND_TRUTH.items():
        img_path = IMAGE_DIR / img_name
        if not img_path.exists():
            print(f"\n!! SKIP: {img_name} not found")
            continue

        print(f"\n{'_'*65}")
        print(f"Testing: {img_name}")
        print(f"  Expected: {len(expected_items)} items -> {expected_items}")

        start = time.time()
        try:
            result = receipt_service.process_receipt(str(img_path))
        except Exception as e:
            print(f"  CRASH: {e}")
            results.append({"image": img_name, "code_pct": 0, "qty_pct": 0, "error": str(e)})
            total_codes_expected += len(expected_items)
            total_qty_expected += len(expected_items)
            continue
        elapsed = time.time() - start

        if not result.get("success"):
            print(f"  FAILED: {result.get('errors', [])}")
            results.append({"image": img_name, "code_pct": 0, "qty_pct": 0, "error": str(result.get("errors"))})
            total_codes_expected += len(expected_items)
            total_qty_expected += len(expected_items)
            continue

        receipt = result["receipt_data"]
        detected = {item["code"]: item["quantity"] for item in receipt["items"]}
        print(f"  Detected: {len(detected)} items -> {detected}")
        print(f"  Time: {elapsed:.1f}s | Engine: {result['metadata'].get('engine_used', '?')}")

        # Score: code detection
        codes_found = 0
        qty_correct = 0
        for code, exp_qty in expected_items.items():
            if code in detected:
                codes_found += 1
                if detected[code] == exp_qty:
                    qty_correct += 1
                    print(f"    [OK] {code}: qty={exp_qty} CORRECT")
                else:
                    print(f"    [!!] {code}: expected qty={exp_qty}, got {detected[code]}")
            else:
                print(f"    [XX] {code}: NOT DETECTED")

        # Check for false positives
        for code in detected:
            if code not in expected_items:
                print(f"    [FP] FALSE POSITIVE: {code} (qty={detected[code]})")

        code_pct = round(codes_found / len(expected_items) * 100)
        qty_pct = round(qty_correct / len(expected_items) * 100) if expected_items else 0

        print(f"  -> Codes: {codes_found}/{len(expected_items)} ({code_pct}%)")
        print(f"  -> Qty:   {qty_correct}/{len(expected_items)} ({qty_pct}%)")

        total_codes_expected += len(expected_items)
        total_codes_detected += codes_found
        total_qty_expected += len(expected_items)
        total_qty_correct += qty_correct
        results.append({"image": img_name, "code_pct": code_pct, "qty_pct": qty_pct})

    # ── SUMMARY ──
    print(f"\n{'='*65}")
    print("  SUMMARY")
    print(f"{'='*65}")
    overall_code = round(total_codes_detected / total_codes_expected * 100) if total_codes_expected else 0
    overall_qty = round(total_qty_correct / total_qty_expected * 100) if total_qty_expected else 0

    for r in results:
        status = "OK" if r.get("code_pct", 0) >= 80 and r.get("qty_pct", 0) >= 60 else "!!"
        err = f" ERROR: {r['error']}" if r.get("error") else ""
        print(f"  [{status}] {r['image']:<25} codes={r['code_pct']:>3}%  qty={r['qty_pct']:>3}%{err}")

    print(f"\n  OVERALL CODE DETECTION: {total_codes_detected}/{total_codes_expected} ({overall_code}%)")
    print(f"  OVERALL QTY ACCURACY:  {total_qty_correct}/{total_qty_expected} ({overall_qty}%)")
    print(f"{'='*65}")

    return overall_code, overall_qty


if __name__ == "__main__":
    code_pct, qty_pct = run_tests()
    sys.exit(0 if code_pct >= 70 else 1)
