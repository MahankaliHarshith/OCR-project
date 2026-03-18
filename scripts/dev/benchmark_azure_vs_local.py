"""
Azure vs Local OCR Accuracy Benchmark
======================================
Tests all 5 receipt images through three OCR modes:
  1. LOCAL only  (EasyOCR)
  2. AZURE only  (Document Intelligence)
  3. HYBRID auto (local quality gate -> Azure -> fallback)

Uses receipt_service.process_receipt() for the full pipeline
(preprocess -> OCR -> parse), same path as the real app.

Switches modes by monkey-patching the singleton engine's .mode attribute
and clearing caches between runs.
"""

import sys
import time
from pathlib import Path

# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Ground truth
GROUND_TRUTH = {
    "receipt_neat.jpg": {"TEW1": 3, "TEW4": 2, "PEPW10": 5, "PEPW20": 1},
    "receipt_messy.jpg": {"TEW10": 2, "TEW20": 4, "PEPW1": 6, "PEPW4": 3},
    "receipt_faded.jpg": {"TEW1": 1, "TEW10": 3, "PEPW1": 2, "PEPW10": 4},
    "receipt_dense.jpg": {
        "TEW1": 2, "TEW4": 5, "TEW10": 1, "TEW20": 3,
        "PEPW1": 4, "PEPW4": 2, "PEPW10": 6, "PEPW20": 1,
    },
    "receipt_dark_ink.jpg": {"PEPW20": 3, "TEW4": 7, "PEPW10": 2, "TEW1": 5, "PEPW4": 1},
}

IMAGE_DIR = ROOT / "test_images"


def score(result, expected):
    """Compare process_receipt output against ground truth."""
    if not result or not result.get("success"):
        return {
            "codes_found": 0, "codes_expected": len(expected),
            "qty_correct": 0, "code_pct": 0, "qty_pct": 0,
            "detected": {}, "false_pos": [],
            "error": str(result.get("errors", "failed")) if result else "crash",
        }

    items = result["receipt_data"]["items"]
    detected = {it["code"]: it["quantity"] for it in items}
    n = len(expected)
    codes_found = sum(1 for c in expected if c in detected)
    qty_ok = sum(1 for c, q in expected.items() if detected.get(c) == q)
    return {
        "codes_found": codes_found, "codes_expected": n,
        "qty_correct": qty_ok,
        "code_pct": round(codes_found / n * 100) if n else 0,
        "qty_pct": round(qty_ok / n * 100) if n else 0,
        "detected": detected,
        "false_pos": [c for c in detected if c not in expected],
    }


def run_mode(label, mode_str, svc, engine):
    """Process all images through a given OCR mode."""
    engine.mode = mode_str

    # Clear image cache so Azure results from previous mode aren't reused
    try:
        engine._image_cache = None
        from app.ocr.image_cache import get_image_cache
        cache = get_image_cache()
        cache._cache.clear()
        cache._save()
    except Exception:
        pass

    print(f"\n{'='*72}")
    print(f"  MODE: {label}  (engine.mode = {mode_str})")
    print(f"{'='*72}")

    per_image = {}
    total_azure = 0

    for img_name, expected in GROUND_TRUTH.items():
        img_path = str(IMAGE_DIR / img_name)
        if not Path(img_path).exists():
            print(f"  SKIP {img_name} -- not found")
            continue

        print(f"\n  Image: {img_name}  ({len(expected)} items expected)")
        t0 = time.time()
        try:
            result = svc.process_receipt(img_path)
            elapsed = int((time.time() - t0) * 1000)
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            print(f"     CRASH: {e}")
            per_image[img_name] = {
                "codes_found": 0, "codes_expected": len(expected),
                "qty_correct": 0, "code_pct": 0, "qty_pct": 0,
                "detected": {}, "false_pos": [], "error": str(e),
                "time_ms": elapsed, "strategy": "error", "azure_pages": 0,
            }
            continue

        s = score(result, expected)
        meta = result.get("metadata", {})
        strategy = meta.get("strategy", meta.get("hybrid_metadata", {}).get("strategy", "?"))
        azure_pg = meta.get("azure_pages_used", 0)
        engine_used = meta.get("engine_used", "?")
        total_azure += azure_pg

        s["time_ms"] = elapsed
        s["strategy"] = strategy
        s["azure_pages"] = azure_pg
        s["engine_used"] = engine_used
        per_image[img_name] = s

        det = s["detected"]
        for code, exp_q in expected.items():
            if code in det:
                ok = "OK" if det[code] == exp_q else f"WRONG(got {det[code]})"
                sym = "+" if det[code] == exp_q else "!"
                print(f"     [{sym}] {code}: exp={exp_q} got={det[code]}  {ok}")
            else:
                print(f"     [-] {code}: exp={exp_q} NOT DETECTED")
        for fp in s["false_pos"]:
            print(f"     [FP] {fp}: qty={det[fp]}  FALSE POSITIVE")

        print(f"     => codes {s['codes_found']}/{s['codes_expected']} ({s['code_pct']}%)  "
              f"qty {s['qty_correct']}/{s['codes_expected']} ({s['qty_pct']}%)  "
              f"engine={engine_used}  strategy={strategy}  azure_pg={azure_pg}  {elapsed}ms")

    # Mode summary
    tc = sum(r["codes_expected"] for r in per_image.values())
    cd = sum(r["codes_found"] for r in per_image.values())
    qo = sum(r["qty_correct"] for r in per_image.values())
    at = sum(r["time_ms"] for r in per_image.values()) / max(len(per_image), 1)
    cp = round(cd / tc * 100) if tc else 0
    qp = round(qo / tc * 100) if tc else 0

    print(f"\n  --- {label} TOTALS ---")
    print(f"  Code detection : {cd}/{tc}  ({cp}%)")
    print(f"  Qty accuracy   : {qo}/{tc}  ({qp}%)")
    print(f"  Azure pages    : {total_azure}")
    print(f"  Avg time       : {at:.0f}ms")

    return {"label": label, "code_pct": cp, "qty_pct": qp,
            "azure_pages": total_azure, "avg_ms": round(at),
            "per_image": per_image}


def main():
    print()
    print("+" + "="*70 + "+")
    print("|  AZURE vs LOCAL OCR ACCURACY BENCHMARK                             |")
    print("|  5 receipt images x 3 modes (local / azure / hybrid-auto)          |")
    print("+" + "="*70 + "+")

    from app.config import AZURE_DOC_INTEL_AVAILABLE
    from app.ocr.hybrid_engine import get_hybrid_engine
    from app.services.receipt_service import ReceiptService

    svc = ReceiptService()
    engine = get_hybrid_engine()

    print(f"\n  Azure Doc Intelligence: {'AVAILABLE' if AZURE_DOC_INTEL_AVAILABLE else 'NOT CONFIGURED'}")
    print(f"  Engine singleton mode : {engine.mode}")
    print(f"  Test images dir       : {IMAGE_DIR}")
    print(f"  Images found          : {sum(1 for f in IMAGE_DIR.iterdir() if f.suffix in ('.jpg','.png'))}")
    print(f"  Ground truth items    : {sum(len(v) for v in GROUND_TRUTH.values())}")

    all_results = {}

    # 1. LOCAL
    all_results["local"] = run_mode("LOCAL (EasyOCR)", "local", svc, engine)

    # 2. AZURE (only if configured)
    if AZURE_DOC_INTEL_AVAILABLE:
        all_results["azure"] = run_mode("AZURE (Doc Intelligence)", "azure", svc, engine)
        # 3. HYBRID AUTO
        all_results["hybrid"] = run_mode("HYBRID (Auto)", "auto", svc, engine)
    else:
        print("\n  *** Azure not configured -- skipping Azure and Hybrid modes ***")

    # COMPARISON TABLE
    print("\n\n" + "="*72)
    print("  FINAL COMPARISON")
    print("="*72)
    header = f"  {'Mode':<30} {'Codes':>7} {'Qty':>7} {'Azure':>7} {'Avg ms':>8}"
    print(header)
    print(f"  {'---'*10}  {'---'*3} {'---'*3} {'---'*3} {'---'*3}")
    for r in all_results.values():
        print(f"  {r['label']:<30} {r['code_pct']:>6}% {r['qty_pct']:>6}% "
              f"{r['azure_pages']:>7} {r['avg_ms']:>7}ms")

    # Per-image breakdown
    print("\n  PER-IMAGE BREAKDOWN")
    print(f"  {'---'*23}")
    for img in GROUND_TRUTH:
        print(f"\n  {img}:")
        for _key, r in all_results.items():
            if img in r["per_image"]:
                d = r["per_image"][img]
                print(f"    {r['label']:<28} code={d['code_pct']:>3}%  qty={d['qty_pct']:>3}%  "
                      f"azure={d.get('azure_pages',0)}  {d.get('time_ms',0)}ms  "
                      f"strat={d.get('strategy','?')}")

    # Improvement summary
    if "azure" in all_results:
        lc = all_results["local"]["code_pct"]
        lq = all_results["local"]["qty_pct"]
        ac = all_results["azure"]["code_pct"]
        aq = all_results["azure"]["qty_pct"]
        print(f"\n  {'='*68}")
        print("  IMPROVEMENT SUMMARY")
        print(f"  {'='*68}")
        print(f"  Azure vs Local : code {ac - lc:+d}pp   qty {aq - lq:+d}pp")
        if "hybrid" in all_results:
            hc = all_results["hybrid"]["code_pct"]
            hq = all_results["hybrid"]["qty_pct"]
            print(f"  Hybrid vs Local: code {hc - lc:+d}pp   qty {hq - lq:+d}pp")
            print(f"  Hybrid Azure pages: {all_results['hybrid']['azure_pages']} "
                  f"(vs {all_results['azure']['azure_pages']} pure Azure)")

    print(f"\n{'='*72}")
    print("  BENCHMARK COMPLETE")
    print(f"{'='*72}\n")

    return all_results


if __name__ == "__main__":
    main()
