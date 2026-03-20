"""
Live Benchmark — measures actual speed and accuracy of the current OCR pipeline.
Runs each uploaded receipt image through: Preprocess → OCR → Parse, timing each stage.
Also queries the database for stored scan results to report accuracy metrics.
"""

import sys
import os
import time
import json
import sqlite3
import statistics
from pathlib import Path

# ── Setup paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("LOG_LEVEL", "WARNING")  # Quiet logs during benchmark

from app.config import UPLOAD_DIR, DATABASE_PATH

# ── Discover images ──────────────────────────────────────────────────────────
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

def find_images():
    images = []
    for f in sorted(UPLOAD_DIR.iterdir()):
        if f.suffix.lower() in IMAGE_EXTENSIONS and f.stat().st_size > 10_000:
            images.append(f)
    return images


# ── Speed Benchmark ──────────────────────────────────────────────────────────
def run_speed_benchmark(images: list[Path]):
    """Run each image through the full pipeline, timing each stage."""
    import cv2
    from app.ocr.preprocessor import ImagePreprocessor
    from app.ocr.engine import OCREngine
    from app.ocr.parser import ReceiptParser
    from app.services.product_service import product_service

    preprocessor = ImagePreprocessor()
    print("  Initializing OCR engine (first-load may be slow)...")
    engine = OCREngine()

    # Get product catalog from DB (or empty dict if no products)
    try:
        catalog = product_service.get_product_code_map()
        print(f"  Product catalog loaded: {len(catalog)} products")
    except Exception:
        catalog = {}
        print("  Product catalog: empty (no products in DB)")
    parser = ReceiptParser(catalog)

    # Warm up the engine with a tiny image so model load doesn't skew timings
    try:
        warmup_img = cv2.imread(str(images[0]))
        if warmup_img is not None:
            small = cv2.resize(warmup_img, (200, 200))
            engine.extract_text_fast(small)
            print("  Engine warmed up ✓")
    except Exception:
        pass

    results = []
    for i, img_path in enumerate(images, 1):
        print(f"\n  [{i}/{len(images)}] {img_path.name}")

        file_kb = img_path.stat().st_size / 1024
        # Read original dimensions before preprocessing
        raw_img = cv2.imread(str(img_path))
        if raw_img is None:
            print(f"    ⚠ Could not read image, skipping")
            continue
        h, w = raw_img.shape[:2]

        # Stage 1: Preprocess (takes file path, returns ndarray + metadata)
        t0 = time.perf_counter()
        processed, proc_info = preprocessor.preprocess(str(img_path))
        t_preprocess = (time.perf_counter() - t0) * 1000

        # Stage 2: OCR (use extract_text which is the full-quality path)
        t1 = time.perf_counter()
        ocr_results = engine.extract_text(processed)
        t_ocr = (time.perf_counter() - t1) * 1000

        # Stage 3: Parse
        t2 = time.perf_counter()
        parsed = parser.parse(ocr_results)
        t_parse = (time.perf_counter() - t2) * 1000

        total_ms = t_preprocess + t_ocr + t_parse

        # Extract metrics
        items = parsed.get("items", [])
        confidences = [it.get("confidence", 0) for it in items if "confidence" in it]
        avg_conf = statistics.mean(confidences) if confidences else 0
        raw_text_count = len(ocr_results) if ocr_results else 0

        result = {
            "file": img_path.name,
            "resolution": f"{w}x{h}",
            "file_kb": round(file_kb, 1),
            "preprocess_ms": round(t_preprocess, 1),
            "ocr_ms": round(t_ocr, 1),
            "parse_ms": round(t_parse, 1),
            "total_ms": round(total_ms, 1),
            "raw_text_blocks": raw_text_count,
            "items_found": len(items),
            "avg_confidence": round(avg_conf, 3),
            "quality_score": proc_info.get("quality_score", 0),
        }
        results.append(result)

        print(f"    {w}x{h} | {file_kb:.0f}KB")
        print(f"    Preprocess: {t_preprocess:>7.1f}ms")
        print(f"    OCR:        {t_ocr:>7.1f}ms")
        print(f"    Parse:      {t_parse:>7.1f}ms")
        print(f"    ─────────────────────")
        print(f"    TOTAL:      {total_ms:>7.1f}ms")
        print(f"    Items: {len(items)} | Blocks: {raw_text_count} | Confidence: {avg_conf:.1%} | Quality: {proc_info.get('quality_score', 0)}")

    return results


# ── Database Accuracy Query ──────────────────────────────────────────────────
def query_db_accuracy():
    """Pull stored scan results from the database for accuracy metrics."""
    db_path = DATABASE_PATH
    if not db_path.exists():
        print(f"\n  ⚠ Database not found at {db_path}")
        return None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get recent receipts with their items
    try:
        cursor.execute("""
            SELECT r.id, r.created_at, r.ocr_confidence_avg,
                   r.total_items, r.bill_total, r.quality_score, r.quality_grade,
                   COUNT(ri.id) as item_count,
                   AVG(ri.ocr_confidence) as avg_item_confidence,
                   SUM(ri.quantity) as total_qty,
                   SUM(CASE WHEN ri.product_code IS NOT NULL AND ri.product_code != '' THEN 1 ELSE 0 END) as coded_items,
                   SUM(CASE WHEN ri.ocr_confidence >= 0.7 THEN 1 ELSE 0 END) as high_conf_items,
                   SUM(CASE WHEN ri.ocr_confidence < 0.5 THEN 1 ELSE 0 END) as low_conf_items,
                   SUM(CASE WHEN ri.manually_edited = 1 THEN 1 ELSE 0 END) as edited_items
            FROM receipts r
            LEFT JOIN receipt_items ri ON r.id = ri.receipt_id
            GROUP BY r.id
            ORDER BY r.created_at DESC
            LIMIT 20
        """)
        receipts = cursor.fetchall()
    except sqlite3.OperationalError as e:
        print(f"\n  ⚠ DB query failed: {e}")
        conn.close()
        return None

    conn.close()
    return receipts


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  HANDWRITTEN RECEIPT SCANNER — LIVE BENCHMARK")
    print("  Pipeline: Preprocess → EasyOCR → Parser")
    print("=" * 65)

    images = find_images()
    if not images:
        print(f"\n  ⚠ No images found in {UPLOAD_DIR}")
        print("  Upload some receipt images first, then re-run.")
        return

    # Filter to original uploads only (skip processed_ duplicates)
    originals = [img for img in images if not img.name.startswith("processed_")]
    if not originals:
        originals = images  # fallback to all

    print(f"\n  Found {len(originals)} receipt image(s) in uploads/")
    print(f"  Upload dir: {UPLOAD_DIR}")

    # ── Speed Benchmark ──
    print("\n" + "─" * 65)
    print("  SPEED BENCHMARK")
    print("─" * 65)

    results = run_speed_benchmark(originals)

    if results:
        print("\n" + "─" * 65)
        print("  SPEED SUMMARY")
        print("─" * 65)

        preprocess_times = [r["preprocess_ms"] for r in results]
        ocr_times = [r["ocr_ms"] for r in results]
        parse_times = [r["parse_ms"] for r in results]
        total_times = [r["total_ms"] for r in results]
        items_found = [r["items_found"] for r in results]
        confidences = [r["avg_confidence"] for r in results if r["avg_confidence"] > 0]

        print(f"\n  {'Stage':<20} {'Avg (ms)':>10} {'Min (ms)':>10} {'Max (ms)':>10}")
        print(f"  {'─'*20} {'─'*10} {'─'*10} {'─'*10}")
        for name, times in [
            ("Preprocess", preprocess_times),
            ("OCR", ocr_times),
            ("Parse", parse_times),
            ("TOTAL", total_times),
        ]:
            avg = statistics.mean(times)
            mn = min(times)
            mx = max(times)
            print(f"  {name:<20} {avg:>10.1f} {mn:>10.1f} {mx:>10.1f}")

        print(f"\n  Avg items/receipt:    {statistics.mean(items_found):.1f}")
        if confidences:
            print(f"  Avg OCR confidence:  {statistics.mean(confidences):.1%}")
        print(f"  Total images tested: {len(results)}")

    # ── Database Accuracy ──
    print("\n" + "─" * 65)
    print("  ACCURACY FROM DATABASE (stored scan results)")
    print("─" * 65)

    receipts = query_db_accuracy()
    if receipts and len(receipts) > 0:
        total_items = sum(r["item_count"] for r in receipts)
        total_coded = sum(r["coded_items"] for r in receipts)
        total_high_conf = sum(r["high_conf_items"] for r in receipts)
        total_low_conf = sum(r["low_conf_items"] for r in receipts)
        total_edited = sum(r["edited_items"] for r in receipts)
        receipt_confs = [r["ocr_confidence_avg"] for r in receipts if r["ocr_confidence_avg"]]
        item_confs = [r["avg_item_confidence"] for r in receipts if r["avg_item_confidence"]]
        total_qty_sum = sum(r["total_qty"] for r in receipts if r["total_qty"])

        print(f"\n  Receipts in DB:            {len(receipts)}")
        print(f"  Total items scanned:       {total_items}")
        print(f"  Items with product code:   {total_coded} / {total_items} ({total_coded/max(total_items,1)*100:.0f}%)")
        print(f"  High confidence (≥70%):    {total_high_conf} / {total_items} ({total_high_conf/max(total_items,1)*100:.0f}%)")
        print(f"  Low confidence (<50%):     {total_low_conf} / {total_items} ({total_low_conf/max(total_items,1)*100:.0f}%)")
        print(f"  Manually edited items:     {total_edited} / {total_items} ({total_edited/max(total_items,1)*100:.0f}%)")
        if receipt_confs:
            print(f"  Avg receipt confidence:    {statistics.mean(receipt_confs):.1%}")
        if item_confs:
            print(f"  Avg item confidence:       {statistics.mean(item_confs):.1%}")
        print(f"  Total quantity scanned:    {total_qty_sum}")

        # Per-receipt breakdown
        print(f"\n  {'Receipt':<8} {'Date':<20} {'Items':>6} {'Coded':>6} {'Edited':>7} {'Conf':>8} {'Quality':>8}")
        print(f"  {'─'*8} {'─'*20} {'─'*6} {'─'*6} {'─'*7} {'─'*8} {'─'*8}")
        for r in receipts:
            rid = r["id"]
            date = r["created_at"][:19] if r["created_at"] else "?"
            items = r["item_count"]
            coded = r["coded_items"]
            edited = r["edited_items"]
            conf = f"{r['ocr_confidence_avg']:.1%}" if r["ocr_confidence_avg"] else "—"
            quality = r["quality_grade"] or "—"
            print(f"  {rid:<8} {date:<20} {items:>6} {coded:>6} {edited:>7} {conf:>8} {quality:>8}")
    else:
        print("\n  No stored scan results found in database.")

    print("\n" + "=" * 65)
    print("  BENCHMARK COMPLETE")
    print("=" * 65)


if __name__ == "__main__":
    main()
