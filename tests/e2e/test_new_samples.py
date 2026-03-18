"""
Test new sample images in tests/sample_inputs for accuracy and speed.
Scans each image, reports: parse time, items found, codes, quantities,
math verification status, and overall summary.
"""
import contextlib
import os
import subprocess
import sys
import time

import requests

# ── Config ──────────────────────────────────────────────────────────
PORT = 8769
BASE = r"c:\Users\mahankali_harshith\OneDrive - EPAM\Desktop\OCR project"
SAMPLE_DIR = os.path.join(BASE, "tests", "sample_inputs")
TIMEOUT = 180  # seconds per scan

os.chdir(BASE)

# ── Start server ────────────────────────────────────────────────────
print("=" * 70)
print("  NEW SAMPLE IMAGES — ACCURACY & SPEED TEST")
print("=" * 70)

# Kill any existing process on the port
try:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    result = s.connect_ex(('127.0.0.1', PORT))
    s.close()
    if result == 0:
        print(f"  Port {PORT} already in use — trying to kill...")
        os.system(f'powershell -c "Get-NetTCPConnection -LocalPort {PORT} -ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"')
        time.sleep(2)
except Exception:
    pass

print(f"\n  Starting server on port {PORT}...")
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

for i in range(40):
    try:
        r = requests.get(f"http://127.0.0.1:{PORT}/api/products", timeout=2)
        if r.status_code == 200:
            print(f"  Server ready after {i+1}s\n")
            break
    except Exception:
        pass
    time.sleep(1)
else:
    print("  ERROR: Server failed to start!")
    server.kill()
    sys.exit(1)

# ── Collect sample files ────────────────────────────────────────────
sample_files = sorted([
    f for f in os.listdir(SAMPLE_DIR)
    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'))
])

if not sample_files:
    print("  No image files found in", SAMPLE_DIR)
    server.kill()
    sys.exit(1)

print(f"  Found {len(sample_files)} sample images:\n")
for f in sample_files:
    size_kb = os.path.getsize(os.path.join(SAMPLE_DIR, f)) / 1024
    print(f"    • {f}  ({size_kb:.0f} KB)")

# ── Known product catalog for validation ────────────────────────────
CATALOG = {
    'ABC': 200, 'XYZ': 220, 'PQR': 650, 'MNO': 45,
    'DEF': 300, 'GHI': 25, 'JKL': 80, 'STU': 180,
    'VWX': 35, 'RST': 120,
    'TEW1': 250, 'TEW4': 850, 'TEW10': 1800, 'TEW20': 3200,
    'PEPW1': 350, 'PEPW4': 1200, 'PEPW10': 2600, 'PEPW20': 4800,
}

VALID_CODES = set(CATALOG.keys())

# ── Run scans ───────────────────────────────────────────────────────
results = []

for idx, fname in enumerate(sample_files, 1):
    fpath = os.path.join(SAMPLE_DIR, fname)
    print(f"\n{'─' * 70}")
    print(f"  [{idx}/{len(sample_files)}]  Scanning: {fname}")
    print(f"{'─' * 70}")

    t_start = time.time()
    try:
        with open(fpath, 'rb') as f:
            r = requests.post(
                f"http://127.0.0.1:{PORT}/api/receipts/scan",
                files={"file": (fname, f, "image/jpeg" if fname.lower().endswith(('.jpg', '.jpeg')) else "image/png")},
                timeout=TIMEOUT,
            )
        t_elapsed = time.time() - t_start
    except requests.exceptions.Timeout:
        t_elapsed = time.time() - t_start
        print(f"    ⚠️  TIMEOUT after {t_elapsed:.1f}s")
        results.append({
            'file': fname, 'status': 'TIMEOUT', 'time': t_elapsed,
            'items': 0, 'codes': [], 'valid_codes': 0, 'total_codes': 0,
        })
        continue
    except Exception as e:
        t_elapsed = time.time() - t_start
        print(f"    ❌ ERROR: {e}")
        results.append({
            'file': fname, 'status': 'ERROR', 'time': t_elapsed,
            'items': 0, 'codes': [], 'valid_codes': 0, 'total_codes': 0,
        })
        continue

    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code}")
        results.append({
            'file': fname, 'status': f'HTTP_{r.status_code}', 'time': t_elapsed,
            'items': 0, 'codes': [], 'valid_codes': 0, 'total_codes': 0,
        })
        continue

    data = r.json()
    rd = data.get('receipt_data')

    if not rd:
        errors = data.get('metadata', {}).get('errors', [])
        print("    ❌ No receipt_data returned")
        if errors:
            print(f"       Errors: {errors}")
        results.append({
            'file': fname, 'status': 'NO_DATA', 'time': t_elapsed,
            'items': 0, 'codes': [], 'valid_codes': 0, 'total_codes': 0,
        })
        continue

    items = rd.get('items', [])
    math = rd.get('math_verification', {})
    metadata = data.get('metadata', {})
    bill_total = rd.get('total_verification', {})
    ocr_engine = metadata.get('ocr_engine', '?')

    # ── Item analysis ──
    parsed_codes = [it.get('code', '?') for it in items]
    valid_codes = [c for c in parsed_codes if c in VALID_CODES]
    invalid_codes = [c for c in parsed_codes if c not in VALID_CODES and c != '?']

    print(f"    ⏱️  Time: {t_elapsed:.2f}s  |  Engine: {ocr_engine}")
    print(f"    📦 Items found: {len(items)}")

    # Print item table
    if items:
        print(f"\n    {'Code':<10} {'Qty':>4} {'Rate':>8} {'Amount':>10} {'Match':>10}")
        print(f"    {'─'*10} {'─'*4} {'─'*8} {'─'*10} {'─'*10}")
        for it in items:
            code = it.get('code', '?')
            qty = it.get('quantity', 0)
            up = it.get('unit_price', 0)
            lt = it.get('line_total', 0)
            mt = it.get('match_type', '?')
            marker = '✅' if code in VALID_CODES else '⚠️'
            print(f"    {marker} {code:<8} {qty:>4} {up:>8.0f} {lt:>10.0f} {mt:>10}")

    # ── Code accuracy ──
    print(f"\n    📊 Code accuracy: {len(valid_codes)}/{len(parsed_codes)} recognized as valid catalog codes")
    if invalid_codes:
        print(f"    ⚠️  Unknown codes: {invalid_codes}")

    # ── Math verification ──
    has_prices = math.get('has_prices', False)
    all_line_ok = math.get('all_line_math_ok', False)
    grand_match = math.get('grand_total_match', False)
    computed_gt = math.get('computed_grand_total', 0)
    ocr_gt = math.get('ocr_grand_total', 0)
    line_checks = math.get('line_checks', [])
    mismatches = math.get('catalog_mismatches', [])

    lines_ok = sum(1 for lc in line_checks if lc.get('math_ok'))
    lines_total = len(line_checks)

    print("\n    🔢 Math Verification:")
    print(f"       has_prices:       {has_prices}")
    print(f"       line math:        {lines_ok}/{lines_total} OK")
    print(f"       all_line_math_ok: {all_line_ok}")
    print(f"       computed_grand:   {computed_gt}")
    print(f"       ocr_grand_total:  {ocr_gt}")
    print(f"       grand_total_match: {grand_match}")

    if line_checks:
        bad_lines = [lc for lc in line_checks if not lc.get('math_ok')]
        if bad_lines:
            print("       ❌ Failed lines:")
            for lc in bad_lines:
                print(f"          {lc.get('code','?')}: {lc.get('qty','?')} × {lc.get('rate','?')} = expected {lc.get('amount_expected','?')}, got OCR {lc.get('amount_ocr','?')}")

    if mismatches:
        print(f"       ⚠️  Catalog price mismatches: {len(mismatches)}")
        for m in mismatches:
            print(f"          {m['code']}: OCR ₹{m['ocr_price']} vs Catalog ₹{m['catalog_price']}")

    # ── Bill total ──
    bt_qty = bill_total.get('ocr_total') or bill_total.get('total_qty_ocr') or 0
    bt_verified = bill_total.get('verified', False) or bill_total.get('verification_status', '') == 'verified'
    computed_qty = bill_total.get('computed_total') or bill_total.get('total_qty_computed') or sum(it.get('quantity', 0) for it in items)
    total_found = bt_qty > 0
    total_status = "✅ MATCH" if (bt_qty > 0 and bt_qty == computed_qty) else ("⚠️ MISMATCH" if bt_qty > 0 else "— no total line")
    print("\n    📝 Bill Total:")
    print(f"       OCR total qty: {bt_qty}  |  Computed from items: {computed_qty}  |  {total_status}")
    print(f"       Verified: {bt_verified}")

    results.append({
        'file': fname,
        'status': 'OK',
        'time': t_elapsed,
        'engine': ocr_engine,
        'items': len(items),
        'codes': parsed_codes,
        'valid_codes': len(valid_codes),
        'total_codes': len(parsed_codes),
        'invalid_codes': invalid_codes,
        'has_prices': has_prices,
        'line_math_ok': lines_ok,
        'line_math_total': lines_total,
        'all_line_ok': all_line_ok,
        'grand_match': grand_match,
        'computed_gt': computed_gt,
        'ocr_gt': ocr_gt,
        'qty_match': bt_qty == computed_qty if bt_qty > 0 else None,
        'total_found': total_found,
        'bt_qty': bt_qty,
        'computed_qty': computed_qty,
    })

# ── Kill server ─────────────────────────────────────────────────────
server.kill()
with contextlib.suppress(Exception):
    server.wait(timeout=5)

# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{'=' * 70}")
print(f"  SUMMARY — {len(sample_files)} IMAGES TESTED")
print(f"{'=' * 70}")

ok_results = [r for r in results if r['status'] == 'OK']
failed_results = [r for r in results if r['status'] != 'OK']

# Speed stats
if ok_results:
    times = [r['time'] for r in ok_results]
    avg_time = sum(times) / len(times)
    min_time = min(times)
    max_time = max(times)
    total_time = sum(times)
    print("\n  ⏱️  SPEED:")
    print(f"     Total scan time:  {total_time:.1f}s")
    print(f"     Average per image: {avg_time:.1f}s")
    print(f"     Fastest:          {min_time:.1f}s")
    print(f"     Slowest:          {max_time:.1f}s")

# Accuracy stats
total_items = sum(r['items'] for r in ok_results)
total_valid = sum(r['valid_codes'] for r in ok_results)
total_all_codes = sum(r['total_codes'] for r in ok_results)
total_line_ok = sum(r.get('line_math_ok', 0) for r in ok_results)
total_line_all = sum(r.get('line_math_total', 0) for r in ok_results)
total_grand_match = sum(1 for r in ok_results if r.get('grand_match'))
total_qty_found = sum(1 for r in ok_results if r.get('total_found'))
total_qty_match = sum(1 for r in ok_results if r.get('qty_match') is True)
total_has_prices = sum(1 for r in ok_results if r.get('has_prices'))

print("\n  📊 ACCURACY:")
print(f"     Images scanned OK:   {len(ok_results)}/{len(results)}")
print(f"     Total items parsed:  {total_items}")
print(f"     Valid catalog codes: {total_valid}/{total_all_codes} ({100*total_valid/max(total_all_codes,1):.0f}%)")
print(f"     Price extraction:    {total_has_prices}/{len(ok_results)} images have prices")
print(f"     Line math correct:   {total_line_ok}/{total_line_all} ({100*total_line_ok/max(total_line_all,1):.0f}%)")
print(f"     Grand total match:   {total_grand_match}/{len(ok_results)} ({100*total_grand_match/max(len(ok_results),1):.0f}%)")
print(f"     Bill total detected: {total_qty_found}/{len(ok_results)} images have total line")
print(f"     Bill qty match:      {total_qty_match}/{total_qty_found} (of detected) ({100*total_qty_match/max(total_qty_found,1):.0f}%)")

if failed_results:
    print("\n  ❌ FAILED SCANS:")
    for r in failed_results:
        print(f"     {r['file']}: {r['status']} ({r['time']:.1f}s)")

# Per-image summary table
print("\n  📋 PER-IMAGE BREAKDOWN:")
print(f"  {'File':<45} {'Time':>6} {'Items':>5} {'Codes':>8} {'LineMath':>10} {'GrandT':>7} {'QtyOK':>6}")
print(f"  {'─'*45} {'─'*6} {'─'*5} {'─'*8} {'─'*10} {'─'*7} {'─'*6}")

for r in results:
    fname = r['file'][:44]
    t = f"{r['time']:.1f}s"
    if r['status'] != 'OK':
        print(f"  {fname:<45} {t:>6} {'─':>5} {'─':>8} {'─':>10} {'─':>7}  {r['status']}")
        continue
    items_s = str(r['items'])
    codes_s = f"{r['valid_codes']}/{r['total_codes']}"
    lm = f"{r.get('line_math_ok',0)}/{r.get('line_math_total',0)}"
    gt = '✅' if r.get('grand_match') else '❌'
    qt = '✅' if r.get('qty_match') is True else ('⚠️' if r.get('qty_match') is False else '—')
    print(f"  {fname:<45} {t:>6} {items_s:>5} {codes_s:>8} {lm:>10} {gt:>7} {qt:>6}")

print(f"\n{'=' * 70}")
print("  TEST COMPLETE")
print(f"{'=' * 70}")
