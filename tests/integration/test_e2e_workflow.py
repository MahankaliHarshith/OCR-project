"""
Full-pipeline scanner workflow test for all generated receipt images.

Tests the same code path as the /api/receipts/scan endpoint:
  preprocessor -> EasyOCR -> parser -> catalog matcher

Reports per-image and aggregate accuracy, speed, and detection rates.
"""

import sys
import time
from pathlib import Path

# -- Expected items per receipt ------------------------------------------------
EXPECTED = {
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


def init_scanner():
    """Initialise exactly the same service stack used by the API."""
    print("  Loading OCR engine + product catalog...")
    t0 = time.perf_counter()
    from app.services.receipt_service import ReceiptService
    svc = ReceiptService()
    elapsed = (time.perf_counter() - t0) * 1000
    n = len(svc.parser.product_catalog)
    print(f"  Ready -- {n} products in catalog  ({elapsed:,.0f} ms init)\n")
    return svc


def scan_image(svc, img_path: Path):
    """Run the full pipeline and return (items_list, timing_dict)."""
    t0 = time.perf_counter()
    result = svc.process_receipt(str(img_path.resolve()))
    total_ms = (time.perf_counter() - t0) * 1000

    # Items are inside receipt_data.items, each has 'code' and 'quantity' keys
    rd = result.get("receipt_data") or {}
    raw_items = rd.get("items", [])

    # Normalise to a common shape for the comparison function
    items = []
    for it in raw_items:
        items.append({
            "product_code": it.get("code", ""),
            "quantity": it.get("quantity", 0),
        })

    meta = result.get("metadata", {})
    timings = {
        "preprocessing_ms": meta.get("preprocessing", {}).get("processing_time_ms", 0)
                            if isinstance(meta.get("preprocessing"), dict) else 0,
        "ocr_ms": meta.get("ocr_time_ms", 0),
        "parsing_ms": meta.get("parse_time_ms", 0),
        "total_ms": total_ms,
    }
    return items, timings


def compare(items, expected):
    """Compare detected vs expected."""
    detected = {}
    for it in items:
        code = (it.get("product_code") or "").upper()
        qty = it.get("quantity", 0)
        if code:
            detected[code] = qty

    code_hits = qty_hits = 0
    total = len(expected)
    details = []

    for code, exp_q in expected.items():
        if code in detected:
            code_hits += 1
            det_q = detected[code]
            q_ok = (det_q == exp_q)
            if q_ok:
                qty_hits += 1
            details.append((code, exp_q, det_q, True, q_ok))
        else:
            details.append((code, exp_q, None, False, False))

    spurious = [c for c in detected if c not in expected]
    return code_hits, qty_hits, total, details, spurious


def print_report(name, code_hits, qty_hits, total, details, spurious, timings):
    """Pretty-print one receipt report."""
    cpct = code_hits / total * 100 if total else 0
    qpct = qty_hits / total * 100 if total else 0

    pre_ms  = timings.get("preprocessing_ms", 0)
    ocr_ms  = timings.get("ocr_ms", 0)
    parse_ms = timings.get("parsing_ms", 0)
    tot_ms  = timings.get("total_ms", 0)

    print(f"\n{'-' * 70}")
    print(f"  [IMAGE]  {name}")
    print(f"{'-' * 70}")
    print(f"  Pipeline  : preprocess {pre_ms:,.0f}ms -> OCR {ocr_ms:,.0f}ms -> "
          f"parse {parse_ms:,.0f}ms -> total {tot_ms:,.0f}ms")
    print(f"  Detection : {code_hits}/{total} codes  ({cpct:.0f}%)")
    print(f"  Accuracy  : {qty_hits}/{total} qty     ({qpct:.0f}%)")
    print()
    print(f"  {'Code':<10} {'Exp':>5} {'Det':>5} {'Code?':>7} {'Qty?':>6}")
    print(f"  {'-'*10} {'-'*5} {'-'*5} {'-'*7} {'-'*6}")
    for code, exp_q, det_q, c_ok, q_ok in details:
        dq = str(int(det_q)) if det_q is not None else "---"
        c_mark = "YES" if c_ok else "NO"
        q_mark = "YES" if q_ok else "NO"
        print(f"  {code:<10} {exp_q:>5} {dq:>5} {c_mark:>7} {q_mark:>6}")
    if spurious:
        print(f"\n  WARNING: Spurious detections: {', '.join(spurious)}")


# ==============================================================================

def main():
    img_dir = Path("test_images")
    images = sorted(img_dir.glob("receipt_*.jpg"))

    print("=" * 70)
    print("  FULL-PIPELINE SCANNER TEST -- 5 Handwritten Receipts")
    print("=" * 70)

    if not images:
        print("  ERROR: No images found in test_images/")
        sys.exit(1)

    print(f"\n  Found {len(images)} test images")
    svc = init_scanner()

    agg_codes = agg_qhits = agg_total = 0
    agg_time = 0.0
    rows = []

    for img_path in images:
        name = img_path.name
        exp = EXPECTED.get(name)
        if not exp:
            print(f"  SKIP {name} (no expected data)")
            continue

        print(f"  Scanning {name} ...", end=" ", flush=True)
        items, timings = scan_image(svc, img_path)
        print(f"done ({timings['total_ms']:,.0f} ms)")

        ch, qh, tot, details, spur = compare(items, exp)
        print_report(name, ch, qh, tot, details, spur, timings)

        agg_codes += ch
        agg_qhits += qh
        agg_total += tot
        agg_time += timings["total_ms"]
        rows.append((name, ch, qh, tot, timings["total_ms"]))

    # -- Aggregate -------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"  AGGREGATE -- {len(rows)} receipts, {agg_total} items")
    print(f"{'=' * 70}")
    cpct = agg_codes / agg_total * 100 if agg_total else 0
    qpct = agg_qhits / agg_total * 100 if agg_total else 0
    print(f"  Code detection : {agg_codes}/{agg_total}  ({cpct:.1f}%)")
    print(f"  Qty accuracy   : {agg_qhits}/{agg_total}  ({qpct:.1f}%)")
    print(f"  Total time     : {agg_time:,.0f} ms")
    if rows:
        print(f"  Avg per receipt: {agg_time / len(rows):,.0f} ms")

    print(f"\n  {'Receipt':<24} {'Codes':>7} {'Qty':>7} {'Time':>10}")
    print(f"  {'-'*24} {'-'*7} {'-'*7} {'-'*10}")
    for name, ch, qh, tot, t in rows:
        print(f"  {name:<24} {ch}/{tot:>4}  {qh}/{tot:>4}  {t:>8,.0f}ms")

    verdict = "PASS" if (agg_codes == agg_total and agg_qhits == agg_total) else "PARTIAL"
    print(f"\n  Overall: {verdict}")
    print()


if __name__ == "__main__":
    main()
