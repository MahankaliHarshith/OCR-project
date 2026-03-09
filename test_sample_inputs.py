"""
Test all sample inputs from tests/sample_inputs/ through the full OCR pipeline.
Reports accuracy (items found, codes correct, quantities correct) and speed.
"""

import sys, os, time, json, threading, requests

# ── Expected results for each sample image ────────────────────────────────────
# Media (2).jpg = Receipt 1: 5 items
# Media (3).jpg = Receipt 2: 3 items
# Media (4).jpg and Media (5).jpg = unknown — we'll discover what's on them
EXPECTED = {
    "Media (2).jpg": {
        "items": [
            {"code": "ABC", "qty": 2},
            {"code": "DEF", "qty": 3},
            {"code": "GHI", "qty": 1},
            {"code": "JKL", "qty": 2},
            {"code": "MNO", "qty": 10},
        ]
    },
    "Media (3).jpg": {
        "items": [
            {"code": "MNO", "qty": 10},
            {"code": "GHI", "qty": 1},
            {"code": "JKL", "qty": 4},
        ]
    },
    "Media (4).jpg": {
        "items": [
            {"code": "XYZ", "qty": 10},
            {"code": "ABC", "qty": 3},
            {"code": "VWX", "qty": 4},
            {"code": "STU", "qty": 2},
        ]
    },
    "Media (5).jpg": {
        "items": [
            {"code": "STU", "qty": 4},
            {"code": "XYZ", "qty": 14},
            {"code": "RST", "qty": 1},
            {"code": "VWX", "qty": 2},
        ]
    },
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(BASE_DIR, "tests", "sample_inputs")
BASE_URL = "http://127.0.0.1:8765"


def start_server():
    """Start test server on port 8765."""
    import uvicorn
    from app.main import app
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def wait_for_server(timeout=15):
    """Wait for test server to be ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE_URL}/", timeout=2)
            if r.status_code == 200:
                return True
        except:
            pass
        time.sleep(0.5)
    return False


def scan_image(filepath):
    """Upload image and get scan results."""
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{BASE_URL}/api/receipts/scan",
            files={"file": (os.path.basename(filepath), f, "image/jpeg")},
            timeout=120,
        )
    return r.status_code, r.json()


def analyze_result(filename, status_code, data, expected_items=None):
    """Analyze scan results and print detailed report."""
    print(f"\n{'='*70}")
    print(f"  📄 {filename}")
    print(f"{'='*70}")

    if status_code != 200:
        print(f"  ❌ HTTP {status_code}")
        print(f"     Error: {data}")
        return {"passed": False, "codes_found": 0, "codes_correct": 0, "qty_correct": 0, "time_ms": 0}

    success = data.get("success", False)
    receipt = data.get("receipt_data", {})
    meta = data.get("metadata", {})
    errors = data.get("errors", [])

    total_ms = meta.get("total_time_ms", 0)
    ocr_ms = meta.get("ocr_time_ms", 0)
    parse_ms = meta.get("parse_time_ms", 0)
    preprocess_ms = meta.get("preprocessing", {}).get("processing_time_ms", 0)
    ocr_passes = meta.get("ocr_passes", "?")

    items = receipt.get("items", [])
    unparsed = receipt.get("unparsed_lines", [])

    print(f"  Status    : {'✅ Success' if success else '❌ Failed'}")
    print(f"  Items     : {len(items)} found")
    print(f"  Unparsed  : {len(unparsed)} lines")
    print(f"  OCR passes: {ocr_passes}")
    print(f"  ⏱  Total  : {total_ms}ms")
    print(f"     Preproc: {preprocess_ms}ms")
    print(f"     OCR    : {ocr_ms}ms")
    print(f"     Parse  : {parse_ms}ms")
    if errors:
        print(f"  ⚠ Errors  : {errors}")

    print(f"\n  {'Code':<8} {'Qty':>6}  {'Product':<30} {'Conf':>6}  {'Review'}")
    print(f"  {'─'*8} {'─'*6}  {'─'*30} {'─'*6}  {'─'*6}")
    found_items = {}
    for item in items:
        code = item.get("code", "?")
        qty = item.get("quantity", 0)
        name = item.get("product", "?")
        conf = item.get("confidence", 0)
        review = "⚠️" if item.get("needs_review") else "✔️"
        print(f"  {code:<8} {qty:>6.1f}  {name:<30} {conf:>5.1%}  {review}")
        found_items[code] = qty

    if unparsed:
        print(f"\n  Unparsed lines:")
        for u in unparsed:
            print(f"    • {u.get('text', '?')!r} (conf={u.get('confidence', 0):.2f})")

    # ── Compare with expected ──
    result = {
        "passed": True,
        "codes_found": len(items),
        "codes_correct": 0,
        "qty_correct": 0,
        "total_expected": 0,
        "time_ms": total_ms,
        "items": found_items,
    }

    if expected_items:
        result["total_expected"] = len(expected_items)
        print(f"\n  ── Accuracy Check ──")
        all_codes_ok = True
        all_qty_ok = True

        for exp in expected_items:
            exp_code = exp["code"]
            exp_qty = exp["qty"]
            actual_qty = found_items.get(exp_code)

            if actual_qty is not None:
                result["codes_correct"] += 1
                code_ok = "✅"
                if abs(actual_qty - exp_qty) < 0.01:
                    result["qty_correct"] += 1
                    qty_ok = "✅"
                else:
                    qty_ok = f"❌ (got {actual_qty}, expected {exp_qty})"
                    all_qty_ok = False
            else:
                code_ok = "❌ MISSING"
                qty_ok = "—"
                all_codes_ok = False

            print(f"    {exp_code}: code={code_ok}  qty={qty_ok}")

        # Check for EXTRA items (false positives)
        expected_codes = {e["code"] for e in expected_items}
        extra = set(found_items.keys()) - expected_codes
        if extra:
            print(f"    ⚠️  Extra items (false positives): {extra}")
            all_codes_ok = False

        result["passed"] = all_codes_ok and all_qty_ok
        code_pct = (result["codes_correct"] / result["total_expected"] * 100) if result["total_expected"] else 0
        qty_pct = (result["qty_correct"] / result["total_expected"] * 100) if result["total_expected"] else 0
        print(f"\n    Code accuracy: {result['codes_correct']}/{result['total_expected']} ({code_pct:.0f}%)")
        print(f"    Qty  accuracy: {result['qty_correct']}/{result['total_expected']} ({qty_pct:.0f}%)")
        print(f"    Overall      : {'✅ PASS' if result['passed'] else '❌ FAIL'}")
    else:
        print(f"\n  ℹ️  No expected results defined — discovery mode")

    return result


def main():
    # Start server
    print("Starting test server on port 8765...")
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    if not wait_for_server():
        print("❌ Server failed to start!")
        sys.exit(1)
    print("Server ready!\n")

    # Find sample images
    if not os.path.isdir(SAMPLE_DIR):
        print(f"❌ Sample directory not found: {SAMPLE_DIR}")
        sys.exit(1)

    images = sorted([
        f for f in os.listdir(SAMPLE_DIR)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
    ])
    print(f"Found {len(images)} sample images: {images}\n")

    # Test each image
    results = {}
    total_time = 0
    for img_name in images:
        img_path = os.path.join(SAMPLE_DIR, img_name)
        expected = EXPECTED.get(img_name, {}).get("items")

        t0 = time.time()
        status_code, data = scan_image(img_path)
        wall_time = int((time.time() - t0) * 1000)

        result = analyze_result(img_name, status_code, data, expected)
        result["wall_time_ms"] = wall_time
        results[img_name] = result
        total_time += result.get("time_ms", 0)

    # ── Summary ──
    print(f"\n{'='*70}")
    print(f"  📊 SUMMARY")
    print(f"{'='*70}")

    total_codes_correct = 0
    total_codes_expected = 0
    total_qty_correct = 0
    total_qty_expected = 0

    for img_name, r in results.items():
        status = "✅ PASS" if r.get("passed") else "❌ FAIL" if r.get("total_expected") else "ℹ️  DISCOVERY"
        print(f"  {img_name:<20} {status:<14} items={r['codes_found']:>2}  time={r['time_ms']:>6}ms")
        if r.get("total_expected"):
            total_codes_correct += r["codes_correct"]
            total_codes_expected += r["total_expected"]
            total_qty_correct += r["qty_correct"]
            total_qty_expected += r["total_expected"]

    if total_codes_expected:
        print(f"\n  Overall Code Accuracy: {total_codes_correct}/{total_codes_expected} ({total_codes_correct/total_codes_expected*100:.0f}%)")
        print(f"  Overall Qty  Accuracy: {total_qty_correct}/{total_qty_expected} ({total_qty_correct/total_qty_expected*100:.0f}%)")
    print(f"  Total Pipeline Time  : {total_time}ms across {len(images)} images")
    print(f"  Avg Time Per Image   : {total_time // max(len(images), 1)}ms")


if __name__ == "__main__":
    main()
