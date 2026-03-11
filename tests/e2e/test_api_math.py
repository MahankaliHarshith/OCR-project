"""Quick API test: verify math_verification data flows through correctly."""
import subprocess, sys, time, os, json, requests

os.chdir(r"c:\Users\mahankali_harshith\OneDrive - EPAM\Desktop\OCR project")

server = subprocess.Popen(
    [r".venv\Scripts\python.exe", "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", "8000"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
)

for i in range(20):
    time.sleep(1)
    try:
        requests.get("http://127.0.0.1:8000/api/products", timeout=2)
        print(f"Server ready after {i+1}s")
        break
    except Exception:
        pass
else:
    print("Server failed to start"); server.kill(); sys.exit(1)

try:
    r = requests.post(
        "http://127.0.0.1:8000/api/receipts/scan",
        files={"file": open("test_images/receipt_neat.jpg", "rb")},
        timeout=120,
    )
    d = r.json()
    mv = d.get("receipt_data", {}).get("math_verification", {})

    print("=" * 60)
    print("  Math Verification API Response")
    print("=" * 60)
    print(f"  has_prices:         {mv.get('has_prices')}")
    print(f"  all_line_math_ok:   {mv.get('all_line_math_ok')}")
    print(f"  computed_grand_total: {mv.get('computed_grand_total')}")
    print(f"  ocr_grand_total:    {mv.get('ocr_grand_total')}")
    print(f"  grand_total_match:  {mv.get('grand_total_match')}")
    print(f"  line_checks count:  {len(mv.get('line_checks', []))}")

    for lc in mv.get("line_checks", []):
        code = lc.get("code", "?")
        qty = lc.get("qty")
        rate = lc.get("rate")
        amt_exp = lc.get("amount_expected")
        amt_ocr = lc.get("amount_ocr")
        ok = lc.get("math_ok")
        print(f"    {code:10s}  qty={qty}  rate={rate}  expected={amt_exp}  ocr={amt_ocr}  ok={ok}")

    mismatches = mv.get("catalog_mismatches", [])
    if mismatches:
        print(f"\n  Catalog mismatches: {len(mismatches)}")
        for m in mismatches:
            print(f"    {m['code']}: OCR ₹{m['ocr_price']} vs Catalog ₹{m['catalog_price']}")
    else:
        print(f"\n  Catalog mismatches: none")

    # Check items have prices
    items = d.get("receipt_data", {}).get("items", [])
    print(f"\n  Items with price data:")
    for it in items:
        code = it.get("code", "?")
        qty = it.get("quantity", 0)
        up = it.get("unit_price", 0)
        lt = it.get("line_total", 0)
        print(f"    {code:10s}  qty={qty}  unit_price={up}  line_total={lt}")

finally:
    server.kill()
    print("\nDone.")
