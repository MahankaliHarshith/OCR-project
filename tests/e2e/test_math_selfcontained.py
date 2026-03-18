"""
Self-contained math verification test.
Starts the server itself, runs tests, then cleans up.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

os.chdir(r"c:\Users\mahankali_harshith\OneDrive - EPAM\Desktop\OCR project")

API = "http://localhost:8000/api/receipts/scan"
VENV_PY = r".venv\Scripts\python.exe"

EXPECTED = {
    "receipt_neat.jpg": [("TEW1",3,250),("TEW4",2,850),("PEPW10",5,2600),("PEPW20",1,4800)],
    "receipt_messy.jpg": [("TEW10",2,1800),("TEW20",4,3200),("PEPW1",6,350),("PEPW4",3,1200)],
    "receipt_faded.jpg": [("TEW1",1,250),("TEW10",3,1800),("PEPW1",2,350),("PEPW10",4,2600)],
    "receipt_dense.jpg": [("TEW1",2,250),("TEW4",5,850),("TEW10",1,1800),("TEW20",3,3200),
                          ("PEPW1",4,350),("PEPW4",2,1200),("PEPW10",6,2600),("PEPW20",1,4800)],
    "receipt_dark_ink.jpg": [("PEPW20",3,4800),("TEW4",7,850),("PEPW10",2,2600),("TEW1",5,250),("PEPW4",1,1200)],
}

# Start server
print("Starting server...")
server = subprocess.Popen(
    [VENV_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
)

# Wait for readiness
for i in range(60):
    try:
        r = requests.get("http://localhost:8000/api/dashboard", timeout=2)
        if r.status_code == 200:
            print(f"Server ready after {i+1}s")
            break
    except Exception:
        pass
    time.sleep(1)
else:
    print("TIMEOUT waiting for server")
    server.terminate()
    sys.exit(1)

print("="*60)
print("  Math / Price Verification Test")
print("="*60)

try:
    for fname, expected_items in EXPECTED.items():
        fpath = Path("test_images") / fname
        print(f"\n📄 {fname}")
        if not fpath.exists():
            print("  ❌ file not found")
            continue

        with open(fpath, "rb") as f:
            resp = requests.post(API, files={"file": f}, timeout=120)
        data = resp.json()
        if not data.get("success"):
            print(f"  ❌ errors: {data.get('errors',[])}")
            continue

        rd = data["receipt_data"]
        items = rd.get("items",[])

        # Code detection
        detected = {it["code"] for it in items}
        expected_codes = {c for c,_,_ in expected_items}
        hit = len(detected & expected_codes)
        print(f"  Codes: {hit}/{len(expected_codes)} ({hit/len(expected_codes)*100:.0f}%)")

        # Qty accuracy
        qty_ok = 0
        for ec, eq, _ in expected_items:
            found = [it for it in items if it["code"]==ec]
            if found and abs(found[0]["quantity"]-eq)<0.5:
                qty_ok += 1
        print(f"  Qty:   {qty_ok}/{len(expected_items)} ({qty_ok/len(expected_items)*100:.0f}%)")

        # Math verification
        mv = rd.get("math_verification") or data.get("metadata",{}).get("math_verification",{})
        hp = mv.get("has_prices", False)
        if hp:
            lc = mv.get("line_checks",[])
            lok = sum(1 for c in lc if c["math_ok"])
            print(f"  Line math: {lok}/{len(lc)} ✅")
            print(f"  Grand total: computed={mv.get('computed_grand_total')}, "
                  f"ocr={mv.get('ocr_grand_total')}, match={mv.get('grand_total_match')}")
            mm = mv.get("catalog_mismatches",[])
            if mm:
                print(f"  \u26a0 Catalog mismatches: {len(mm)}")
        else:
            iwp = sum(1 for it in items if it.get("unit_price",0)>0)
            print(f"  Prices: {iwp}/{len(items)} items got catalog prices (OCR didn't extract prices)")

        # Show each item
        for it in items:
            r = it.get("unit_price",0)
            t = it.get("line_total",0)
            s = it.get("price_source","")
            print(f"    {it['code']:8s} qty={it['quantity']:>2}  rate={r:>6}  total={t:>8}  [{s}]")

finally:
    print("\n\nShutting down server...")
    server.terminate()
    server.wait(timeout=10)
    print("Done.")
