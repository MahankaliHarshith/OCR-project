"""Pipeline benchmark — identifies speed bottlenecks & accuracy gaps."""
import time, cv2, numpy as np, sys, os

# Make sure we can import app modules
sys.path.insert(0, os.path.dirname(__file__))

from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.engine import OCREngine, get_ocr_engine
from app.ocr.parser import ReceiptParser
from app.services.product_service import product_service

IMG = "uploads/upload_20260221_182218.jpg"

def benchmark():
    print("=" * 60)
    print("  HANDWRITTEN RECEIPT SCANNER — PIPELINE BENCHMARK")
    print("=" * 60)
    print()

    # ── 1. PREPROCESS ────────────────────────────────────────
    pp = ImagePreprocessor()
    t0 = time.time()
    processed, meta = pp.preprocess(IMG)
    t_pp = time.time() - t0
    print(f"[1] PREPROCESS:  {t_pp*1000:>7.0f} ms")
    print(f"    In:  {meta['original_size']}")
    print(f"    Out: {meta['processed_size']}")
    print(f"    Quality: {meta['quality']['score']:.1f}")
    print()

    # ── 2. OCR ENGINE INIT ───────────────────────────────────
    t0 = time.time()
    engine = get_ocr_engine()
    t_init = time.time() - t0
    cached = t_init < 0.5
    print(f"[2] ENGINE INIT: {t_init*1000:>7.0f} ms {'(CACHED)' if cached else '(COLD START)'}")
    print()

    # ── 3a. OCR ON COLOR ─────────────────────────────────────
    color = cv2.imread(IMG)
    h, w = color.shape[:2]
    from app.config import IMAGE_MAX_DIMENSION
    if max(h, w) > IMAGE_MAX_DIMENSION:
        scale = IMAGE_MAX_DIMENSION / max(h, w)
        color = cv2.resize(color, None, fx=scale, fy=scale)
    t0 = time.time()
    color_res = engine.extract_text(color)
    t_color = time.time() - t0
    print(f"[3a] OCR COLOR:  {t_color*1000:>7.0f} ms \u2192 {len(color_res)} detections")

    # ── 3b. OCR ON GRAYSCALE ─────────────────────────────────
    # Use the processed (not cropped) image for fair comparison
    t0 = time.time()
    gray_res = engine.extract_text(processed)
    t_gray = time.time() - t0
    print(f"[3b] OCR GRAY:   {t_gray*1000:>7.0f} ms → {len(gray_res)} detections")
    print(f"[3]  OCR TOTAL:  {(t_color+t_gray)*1000:>7.0f} ms  (2 passes)")
    print()

    # ── 4. PARSE ─────────────────────────────────────────────
    catalog = product_service.get_product_code_map()
    parser = ReceiptParser(catalog)
    primary = gray_res if len(gray_res) >= len(color_res) else color_res
    t0 = time.time()
    result = parser.parse(primary)
    t_parse = time.time() - t0
    print(f"[4] PARSE:       {t_parse*1000:>7.0f} ms → {result['total_items']} items")
    for item in result["items"]:
        print(f"    {item['code']:>5} × {item['quantity']:<5}  conf={item['confidence']:.3f}  match={item['match_type']}")
    print()

    # ── SUMMARY ──────────────────────────────────────────────
    total = t_pp + t_color + t_gray + t_parse
    print("=" * 60)
    print(f"  TOTAL (excl. engine init): {total*1000:.0f} ms")
    print(f"    Preprocess : {t_pp*1000:>6.0f} ms  ({t_pp/total*100:>5.1f}%)")
    print(f"    OCR color  : {t_color*1000:>6.0f} ms  ({t_color/total*100:>5.1f}%)")
    print(f"    OCR gray   : {t_gray*1000:>6.0f} ms  ({t_gray/total*100:>5.1f}%)")
    print(f"    Parse      : {t_parse*1000:>6.0f} ms  ({t_parse/total*100:>5.1f}%)")
    print("=" * 60)
    print()

    # ── BOTTLENECK ANALYSIS ──────────────────────────────────
    print("BOTTLENECK ANALYSIS:")
    if (t_color + t_gray) / total > 0.85:
        print("  🔴 OCR is >85% of pipeline time — #1 optimization target")
    print(f"  • Dual-pass OCR doubles OCR time ({t_color*1000:.0f}+{t_gray*1000:.0f}={int((t_color+t_gray)*1000)}ms)")
    print(f"  • Color image: {color.shape} = {color.nbytes/1024/1024:.1f}MB")
    print(f"  • Gray  image: {processed.shape} = {processed.nbytes/1024/1024:.1f}MB")

    # Check OCR engine parameters
    print()
    print("OCR ENGINE PARAMETERS:")
    from app.config import (OCR_CANVAS_SIZE, OCR_MAG_RATIO, OCR_MIN_SIZE,
                            OCR_TEXT_THRESHOLD, OCR_LOW_TEXT, IMAGE_MAX_DIMENSION)
    print(f"  canvas_size = {OCR_CANVAS_SIZE}  (smaller=faster)")
    print(f"  mag_ratio   = {OCR_MAG_RATIO}  (smaller=faster)")
    print(f"  min_size    = {OCR_MIN_SIZE}")
    print(f"  text_thresh = {OCR_TEXT_THRESHOLD}")
    print(f"  low_text    = {OCR_LOW_TEXT}")
    print(f"  max_dim     = {IMAGE_MAX_DIMENSION}")

    # ── ACCURACY ANALYSIS ────────────────────────────────────
    print()
    print("ACCURACY ANALYSIS:")
    expected = {"ABC": 2, "DEF": 3, "GHI": 1, "JKL": 2, "MNO": 10}
    actual = {item["code"]: item["quantity"] for item in result["items"]}
    all_correct = True
    for code, exp_qty in expected.items():
        act_qty = actual.get(code)
        ok = act_qty == exp_qty
        if not ok:
            all_correct = False
        mark = "✅" if ok else "❌"
        print(f"  {mark} {code}: expected={exp_qty}, actual={act_qty}")
    print(f"  {'ALL CORRECT' if all_correct else 'SOME WRONG'}")
    print(f"  Avg confidence: {result['avg_confidence']:.4f}")
    print(f"  Needs review:   {result['needs_review']}")
    print(f"  Unparsed lines: {len(result['unparsed_lines'])}")

    return total, all_correct

if __name__ == "__main__":
    benchmark()
