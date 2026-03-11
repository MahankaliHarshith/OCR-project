"""Edge case regression test: validates codes, quantities, and total verification."""
import subprocess, sys, time, requests, os

PORT = 8771
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

for _ in range(40):
    try:
        r = requests.get(f'http://127.0.0.1:{PORT}/api/products', timeout=2)
        if r.status_code == 200:
            break
    except Exception:
        pass
    time.sleep(1)
else:
    print('Server failed to start')
    proc.kill()
    sys.exit(1)

print('Server ready\n')

# Ground truth from generate_edge_case_receipts.py
EDGE_EXPECTED = {
    'edge_single_item.jpg':          {'items': {'TEW1': 5}, 'total': 5},
    'edge_no_total.jpg':             {'items': {'TEW4': 3, 'PEPW10': 2, 'TEW20': 1}, 'total': None},
    'edge_large_qty.jpg':            {'items': {'TEW10': 15, 'TEW20': 20, 'PEPW4': 10}, 'total': 45},
    'edge_alpha_codes.jpg':          {'items': {'ABC': 2, 'DEF': 3, 'GHI': 4}, 'total': 9},
    'edge_all_qty1.jpg':             {'items': {'TEW1': 1, 'TEW4': 1, 'PEPW1': 1, 'PEPW4': 1}, 'total': 4},
    'edge_double_digit.jpg':         {'items': {'TEW1': 10, 'PEPW20': 12, 'TEW10': 15}, 'total': 37},
    'edge_many_items.jpg':           {'items': {'TEW1': 2, 'TEW4': 3, 'TEW10': 1, 'TEW20': 4, 'PEPW1': 5, 'PEPW4': 2, 'PEPW10': 3, 'PEPW20': 1}, 'total': 21},
    'edge_total_items_confusion.jpg':{'items': {'TEW1': 3, 'PEPW10': 2, 'TEW20': 3}, 'total': 8},
    'edge_mixed_codes.jpg':          {'items': {'ABC': 3, 'TEW10': 2, 'DEF': 1, 'PEPW1': 4}, 'total': 10},
    'edge_high_total.jpg':           {'items': {'TEW1': 10, 'TEW4': 12, 'TEW10': 8, 'PEPW20': 15, 'PEPW10': 10}, 'total': 55},
}

test_dir = 'test_images'
tc = tq = tt = tc_ok = tq_ok = tt_ok = 0

print(f"{'='*70}")
print(f"  EDGE CASE REGRESSION TEST — {len(EDGE_EXPECTED)} images")
print(f"{'='*70}")

for fname, expected in sorted(EDGE_EXPECTED.items()):
    fpath = os.path.join(test_dir, fname)
    if not os.path.exists(fpath):
        print(f'\n  {fname}: FILE NOT FOUND')
        continue

    with open(fpath, 'rb') as f:
        r = requests.post(
            f'http://127.0.0.1:{PORT}/api/receipts/scan',
            files={'file': (fname, f, 'image/jpeg')},
            timeout=180,
        )

    if r.status_code != 200:
        print(f'\n  {fname}: HTTP {r.status_code}')
        continue

    data = r.json()
    rd = data.get('receipt_data', {})
    items = rd.get('items', [])
    tv = rd.get('total_verification', {})

    parsed = {it['code']: it['quantity'] for it in items}
    exp_items = expected['items']
    exp_total = expected['total']

    # Code check
    codes_ok = sum(1 for c in exp_items if c in parsed)
    tc += len(exp_items)
    tc_ok += codes_ok

    # Qty check
    qty_ok = sum(1 for c, q in exp_items.items() if c in parsed and abs(parsed[c] - q) < 0.1)
    tq += len(exp_items)
    tq_ok += qty_ok

    # Total check
    ocr_total = tv.get('ocr_total') or tv.get('total_qty_ocr') or 0
    if exp_total is not None:
        tt += 1
        if abs(ocr_total - exp_total) < 0.1:
            tt_ok += 1
            total_status = '✅'
        else:
            total_status = f'❌ (OCR={ocr_total}, exp={exp_total})'
    else:
        total_status = '— (no total expected)'

    # Report
    wrong = []
    for c, q in exp_items.items():
        if c not in parsed:
            wrong.append(f'{c} MISSING')
        elif abs(parsed[c] - q) >= 0.1:
            wrong.append(f'{c}={parsed[c]} exp={q}')

    status = '✅' if not wrong else '⚠️'
    print(f'\n  {fname}:')
    print(f'    Codes: {codes_ok}/{len(exp_items)}  Qty: {qty_ok}/{len(exp_items)}  Total: {total_status}')
    if wrong:
        print(f'    Issues: {", ".join(wrong)}')

proc.kill()
try:
    proc.wait(timeout=5)
except Exception:
    pass

print(f'\n{"="*70}')
print(f'  EDGE CASE SUMMARY')
print(f'{"="*70}')
print(f'  CODES: {tc_ok}/{tc} ({100*tc_ok//max(tc,1)}%)')
print(f'  QTY:   {tq_ok}/{tq} ({100*tq_ok//max(tq,1)}%)')
print(f'  TOTAL: {tt_ok}/{tt} ({100*tt_ok//max(tt,1)}%) of images with total lines')
print(f'{"="*70}')
