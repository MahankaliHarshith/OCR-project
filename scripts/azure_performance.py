"""Check how Azure Document Intelligence performed across all calls."""
import json
import sqlite3
import statistics
from pathlib import Path
import sys, os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.config import DATABASE_PATH

print("=" * 70)
print("  AZURE DOCUMENT INTELLIGENCE — PERFORMANCE ANALYSIS")
print("=" * 70)

# ── 1. Usage log analysis ──
usage_file = Path(__file__).resolve().parent.parent / "data" / "azure_usage.json"
with open(usage_file, "r", encoding="utf-8") as f:
    data = json.load(f)

print("\n[1] AZURE CALL LOG (all calls ever made)")
print("-" * 70)
total_calls = 0
total_cost = 0.0
all_calls = []

for day, info in sorted(data.get("days", {}).items()):
    calls = info.get("calls", [])
    print(f"\n  {day} — {len(calls)} call(s)")
    for i, c in enumerate(calls, 1):
        ts = c.get("timestamp", "")
        model = c.get("model", "?")
        success = c.get("success", False)
        cost = c.get("cost", 0)
        pages = c.get("pages", 0)
        status = "OK" if success else "FAIL"
        time_part = ts[11:19] if len(ts) > 19 else ts
        print(f"    [{i}] {time_part}  {model:<20}  {status}  {pages} pg  ${cost:.4f}")
        total_calls += 1
        total_cost += cost
        all_calls.append(c)

receipt_calls = sum(1 for c in all_calls if "receipt" in c.get("model", ""))
read_calls = sum(1 for c in all_calls if "read" in c.get("model", ""))
success_calls = sum(1 for c in all_calls if c.get("success"))
fail_calls = total_calls - success_calls

print(f"\n  SUMMARY:")
print(f"    Total calls:       {total_calls}")
print(f"    Successful:        {success_calls}")
print(f"    Failed:            {fail_calls}")
print(f"    Receipt model:     {receipt_calls} calls (${receipt_calls * 0.01:.2f})")
print(f"    Read model:        {read_calls} calls (${read_calls * 0.0015:.4f})")
print(f"    Total cost:        ${total_cost:.4f}")
print(f"    Free tier used:    {total_calls}/500 pages ({total_calls/5:.0f}%)")
print(f"    Free tier left:    {500 - total_calls} pages")

# ── 2. Cross-reference with DB: which receipts used Azure? ──
print("\n\n[2] RECEIPTS SCANNED WITH AZURE (from DB)")
print("-" * 70)

db_path = DATABASE_PATH
if not db_path.exists():
    print(f"  Database not found at {db_path}")
else:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get receipts from the dates Azure was used
    azure_dates = sorted(data.get("days", {}).keys())
    print(f"  Azure was used on: {', '.join(azure_dates)}")

    for adate in azure_dates:
        cursor.execute("""
            SELECT r.id, r.receipt_number, r.created_at, r.ocr_confidence_avg,
                   r.total_items, r.bill_total, r.quality_score, r.quality_grade,
                   r.processing_status,
                   COUNT(ri.id) as item_count,
                   AVG(ri.ocr_confidence) as avg_item_conf,
                   SUM(CASE WHEN ri.ocr_confidence >= 0.7 THEN 1 ELSE 0 END) as hi_conf,
                   SUM(CASE WHEN ri.ocr_confidence < 0.5 THEN 1 ELSE 0 END) as lo_conf,
                   SUM(CASE WHEN ri.manually_edited = 1 THEN 1 ELSE 0 END) as edited
            FROM receipts r
            LEFT JOIN receipt_items ri ON r.id = ri.receipt_id
            WHERE r.scan_date = ?
            GROUP BY r.id
            ORDER BY r.created_at
        """, (adate,))
        rows = cursor.fetchall()

        n_azure_calls = len(data["days"][adate].get("calls", []))
        print(f"\n  {adate}: {len(rows)} receipt(s) in DB, {n_azure_calls} Azure call(s)")

        for r in rows:
            conf = f"{r['ocr_confidence_avg']:.1%}" if r["ocr_confidence_avg"] else "n/a"
            item_conf = f"{r['avg_item_conf']:.1%}" if r["avg_item_conf"] else "n/a"
            grade = r["quality_grade"] or "-"
            edited = r["edited"] or 0
            items = r["item_count"] or 0
            hi = r["hi_conf"] or 0
            lo = r["lo_conf"] or 0
            bill = r["bill_total"] or 0

            print(f"    Receipt #{r['id']} | {r['created_at'][:19]}")
            print(f"      Items: {items} | Conf: {conf} | Item Conf: {item_conf}")
            print(f"      High conf: {hi}/{items} | Low conf: {lo}/{items} | Edited: {edited}/{items}")
            print(f"      Bill total: {bill:.0f} | Quality: {grade} | Status: {r['processing_status']}")

    # ── 3. Compare Azure-era vs non-Azure receipts ──
    print("\n\n[3] AZURE vs LOCAL-ONLY PERFORMANCE COMPARISON")
    print("-" * 70)

    # Receipts from Azure days
    azure_date_str = ",".join(f"'{d}'" for d in azure_dates)
    cursor.execute(f"""
        SELECT r.ocr_confidence_avg, r.total_items, r.quality_grade,
               COUNT(ri.id) as item_count,
               AVG(ri.ocr_confidence) as avg_item_conf,
               SUM(CASE WHEN ri.ocr_confidence >= 0.7 THEN 1 ELSE 0 END) as hi,
               SUM(CASE WHEN ri.ocr_confidence < 0.5 THEN 1 ELSE 0 END) as lo,
               SUM(CASE WHEN ri.manually_edited = 1 THEN 1 ELSE 0 END) as edited
        FROM receipts r
        LEFT JOIN receipt_items ri ON r.id = ri.receipt_id
        WHERE r.scan_date IN ({azure_date_str})
          AND r.total_items > 0
        GROUP BY r.id
    """)
    azure_rows = cursor.fetchall()

    # Receipts from non-Azure days (local only)
    cursor.execute(f"""
        SELECT r.ocr_confidence_avg, r.total_items, r.quality_grade,
               COUNT(ri.id) as item_count,
               AVG(ri.ocr_confidence) as avg_item_conf,
               SUM(CASE WHEN ri.ocr_confidence >= 0.7 THEN 1 ELSE 0 END) as hi,
               SUM(CASE WHEN ri.ocr_confidence < 0.5 THEN 1 ELSE 0 END) as lo,
               SUM(CASE WHEN ri.manually_edited = 1 THEN 1 ELSE 0 END) as edited
        FROM receipts r
        LEFT JOIN receipt_items ri ON r.id = ri.receipt_id
        WHERE r.scan_date NOT IN ({azure_date_str})
          AND r.total_items > 0
        GROUP BY r.id
    """)
    local_rows = cursor.fetchall()

    def stats(rows, label):
        if not rows:
            print(f"\n  {label}: No data")
            return
        confs = [r["ocr_confidence_avg"] for r in rows if r["ocr_confidence_avg"]]
        item_confs = [r["avg_item_conf"] for r in rows if r["avg_item_conf"]]
        total_items = sum(r["item_count"] for r in rows)
        total_hi = sum(r["hi"] or 0 for r in rows)
        total_lo = sum(r["lo"] or 0 for r in rows)
        total_edited = sum(r["edited"] or 0 for r in rows)

        avg_conf = statistics.mean(confs) if confs else 0
        avg_item_conf = statistics.mean(item_confs) if item_confs else 0
        hi_pct = total_hi / max(total_items, 1) * 100
        lo_pct = total_lo / max(total_items, 1) * 100
        edit_pct = total_edited / max(total_items, 1) * 100

        print(f"\n  {label}:")
        print(f"    Receipts:           {len(rows)}")
        print(f"    Total items:        {total_items}")
        print(f"    Avg receipt conf:   {avg_conf:.1%}")
        print(f"    Avg item conf:      {avg_item_conf:.1%}")
        print(f"    High conf items:    {total_hi}/{total_items} ({hi_pct:.0f}%)")
        print(f"    Low conf items:     {total_lo}/{total_items} ({lo_pct:.0f}%)")
        print(f"    Manually edited:    {total_edited}/{total_items} ({edit_pct:.0f}%)")

    stats(azure_rows, "AZURE-ASSISTED SCANS (Mar 12, 18, 19)")
    stats(local_rows, "LOCAL-ONLY SCANS (all other days)")

    # ── 4. Check if any corrections were made ──
    print("\n\n[4] OCR CORRECTIONS (learning from user edits)")
    print("-" * 70)
    try:
        cursor.execute("SELECT COUNT(*) as cnt FROM ocr_corrections")
        corr_count = cursor.fetchone()["cnt"]
        print(f"  Total corrections stored: {corr_count}")

        if corr_count > 0:
            cursor.execute("""
                SELECT original_code, corrected_code, original_confidence, new_confidence
                FROM ocr_corrections
                ORDER BY id DESC
                LIMIT 10
            """)
            for c in cursor.fetchall():
                orig_c = f"{c['original_confidence']:.1%}" if c["original_confidence"] else "n/a"
                new_c = f"{c['new_confidence']:.1%}" if c["new_confidence"] else "n/a"
                print(f"    {c['original_code']:>8} -> {c['corrected_code']:<8}  (conf: {orig_c} -> {new_c})")
    except Exception as e:
        print(f"  Could not query corrections: {e}")

    conn.close()

print("\n" + "=" * 70)
print("  ANALYSIS COMPLETE")
print("=" * 70)
