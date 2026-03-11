"""Deep audit test: validates codes, quantities, math, and grand totals."""
import subprocess, sys, time, requests, os

PORT = 8769
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

test_dir = 'test_images'
FILES = [
    'receipt_neat.jpg',
    'receipt_messy.jpg',
    'receipt_faded.jpg',
    'receipt_dense.jpg',
    'receipt_dark_ink.jpg',
]
EXPECTED = {
    'receipt_neat.jpg':     (['TEW1','TEW4','PEPW10','PEPW20'], [3,2,5,1]),
    'receipt_messy.jpg':    (['TEW10','TEW20','PEPW1','PEPW4'], [2,4,6,3]),
    'receipt_faded.jpg':    (['TEW1','TEW10','PEPW1','PEPW10'], [1,3,2,4]),
    'receipt_dense.jpg':    (['TEW1','TEW4','TEW10','TEW20','PEPW1','PEPW4','PEPW10','PEPW20'], [2,5,1,3,4,2,6,1]),
    'receipt_dark_ink.jpg': (['PEPW20','TEW4','PEPW10','TEW1','PEPW4'], [3,7,2,5,1]),
}

tc = tq = tm = tc_ok = tq_ok = tm_ok = 0

for fname in FILES:
    fpath = os.path.join(test_dir, fname)
    if not os.path.exists(fpath):
        print(f'  {fname}: FILE NOT FOUND')
        continue

    with open(fpath, 'rb') as f:
        r = requests.post(
            f'http://127.0.0.1:{PORT}/api/receipts/scan',
            files={'file': (fname, f, 'image/jpeg')},
            timeout=180,
        )

    if r.status_code != 200:
        print(f'  {fname}: HTTP {r.status_code}')
        continue

    data = r.json()
    rd = data.get('receipt_data')
    if not rd:
        print(f'  {fname}: FAILED - receipt_data is None')
        errors = data.get('metadata', {}).get('errors', [])
        if errors:
            print(f'    Errors: {errors}')
        continue

    items = rd.get('items', [])
    math = rd.get('math_verification', {})

    parsed_codes = [it['code'] for it in items]
    parsed_qtys = {it['code']: it['quantity'] for it in items}

    exp_codes, exp_qtys = EXPECTED[fname]

    codes_ok = sum(1 for c in exp_codes if c in parsed_codes)
    tc_ok += codes_ok
    tc += len(exp_codes)

    qty_ok = 0
    for c, q in zip(exp_codes, exp_qtys):
        if c in parsed_qtys and abs(parsed_qtys[c] - q) < 0.1:
            qty_ok += 1
    tq_ok += qty_ok
    tq += len(exp_qtys)

    lcs = math.get('line_checks', [])
    mp = sum(1 for lc in lcs if lc.get('math_ok'))
    tm_ok += mp
    tm += len(lcs)

    gt = math.get('grand_total_match', False)

    wrong = []
    for c, q in zip(exp_codes, exp_qtys):
        a = parsed_qtys.get(c)
        if a is None:
            wrong.append(f'{c} MISSING')
        elif abs(a - q) >= 0.1:
            wrong.append(f'{c}={a} exp={q}')

    print(f'  {fname}:')
    print(f'    Codes: {codes_ok}/{len(exp_codes)}  Qty: {qty_ok}/{len(exp_qtys)}  Math: {mp}/{len(lcs)}  GrandTotal: {"✅" if gt else "❌"}')
    if wrong:
        print(f'    Issues: {", ".join(wrong)}')

print()
print('=' * 40)
print(f'CODES: {tc_ok}/{tc} ({100*tc_ok/max(tc,1):.0f}%)')
print(f'QTY:   {tq_ok}/{tq} ({100*tq_ok/max(tq,1):.0f}%)')
print(f'MATH:  {tm_ok}/{tm} ({100*tm_ok/max(tm,1):.0f}%)')
print('=' * 40)

proc.kill()
print('\nDone.')
