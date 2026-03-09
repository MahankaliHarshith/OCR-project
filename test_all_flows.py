"""
Comprehensive end-to-end test for all key flows.
Run with: python test_all_flows.py
"""
import os
import sys
import time
import json
import requests
import threading
import uvicorn

BASE = "http://127.0.0.1:8765"
PASS = 0
FAIL = 0
ERRORS = []

def check(label, condition, detail=""):
    global PASS, FAIL, ERRORS
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        msg = f"  ❌ {label}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)

def start_server():
    from app.main import app
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    server.run()

def wait_for_server(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE}/", timeout=2)
            if r.status_code == 200:
                return True
        except:
            pass
        time.sleep(0.5)
    return False

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: Homepage & Static Assets
# ═══════════════════════════════════════════════════════════════════════════════
def test_1_homepage_and_static():
    print("\n═══ Test 1: Homepage & Static Assets ═══")
    r = requests.get(f"{BASE}/")
    check("Homepage loads (200)", r.status_code == 200)
    check("HTML has scan tab", "tab-scan" in r.text)
    check("HTML has receipts tab", "tab-receipts" in r.text)
    check("HTML has catalog tab", "tab-catalog" in r.text)
    check("HTML has upload area", "dropZone" in r.text or "drop-zone" in r.text)

    r2 = requests.get(f"{BASE}/static/app.js")
    check("app.js loads", r2.status_code == 200)
    check("app.js has processFile", "processFile" in r2.text)

    r3 = requests.get(f"{BASE}/static/styles.css")
    check("styles.css loads", r3.status_code == 200)

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: Products Catalog - List
# ═══════════════════════════════════════════════════════════════════════════════
def test_2_products_catalog():
    print("\n═══ Test 2: Products / Catalog ═══")
    r = requests.get(f"{BASE}/api/products")
    check("GET /api/products (200)", r.status_code == 200)
    data = r.json()
    check("Response has 'products' key", "products" in data)
    products = data.get("products", [])
    check("10 default products loaded", len(products) == 10, f"got {len(products)}")
    codes = [p["product_code"] for p in products]
    for c in ["ABC", "DEF", "GHI", "JKL", "MNO"]:
        check(f"Product {c} exists", c in codes)

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: Add Product
# ═══════════════════════════════════════════════════════════════════════════════
def test_3_add_product():
    print("\n═══ Test 3: Add Product ═══")
    payload = {"product_code": "TST", "product_name": "Test Product", "category": "Testing", "unit": "Piece"}
    r = requests.post(f"{BASE}/api/products", json=payload)
    check("POST /api/products (200)", r.status_code == 200)
    data = r.json()
    check("Response has product", "product" in data)

    # Verify it appears in list
    r2 = requests.get(f"{BASE}/api/products")
    codes = [p["product_code"] for p in r2.json()["products"]]
    check("TST now in product list", "TST" in codes)

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4: Edit Product
# ═══════════════════════════════════════════════════════════════════════════════
def test_4_edit_product():
    print("\n═══ Test 4: Edit Product ═══")
    payload = {"product_name": "Test Product EDITED"}
    r = requests.put(f"{BASE}/api/products/TST", json=payload)
    check("PUT /api/products/TST (200)", r.status_code == 200)

    # Verify updated
    r2 = requests.get(f"{BASE}/api/products/TST")
    check("GET product by code (200)", r2.status_code == 200)
    prod = r2.json()
    check("Product name updated", prod.get("product_name") == "Test Product EDITED",
          f"got: {prod.get('product_name')}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5: Search Products
# ═══════════════════════════════════════════════════════════════════════════════
def test_5_search_products():
    print("\n═══ Test 5: Search Products ═══")
    r = requests.get(f"{BASE}/api/products/search", params={"q": "Paint"})
    check("Search 'Paint' (200)", r.status_code == 200)
    results = r.json().get("products", [])
    check("Search finds results", len(results) >= 1, f"got {len(results)}")
    if results:
        check("ABC found in results", any(p["product_code"] == "ABC" for p in results))

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6: Delete Product
# ═══════════════════════════════════════════════════════════════════════════════
def test_6_delete_product():
    print("\n═══ Test 6: Delete Product ═══")
    r = requests.delete(f"{BASE}/api/products/TST")
    check("DELETE /api/products/TST (200)", r.status_code == 200)

    # Verify gone from active list
    r2 = requests.get(f"{BASE}/api/products")
    codes = [p["product_code"] for p in r2.json()["products"]]
    check("TST removed from active products", "TST" not in codes)

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7: Scan Receipt (with real image)
# ═══════════════════════════════════════════════════════════════════════════════
def test_7_scan_receipt():
    print("\n═══ Test 7: Scan Receipt (with real image) ═══")
    # Find a test image — prefer original uploads over processed
    img_path = None
    for name in ["Media (2).jpg", "Media (3).jpg"]:
        for root_dir in [".", "uploads", "test_images"]:
            candidate = os.path.join(root_dir, name)
            if os.path.exists(candidate):
                img_path = candidate
                break
        if img_path:
            break

    if not img_path:
        uploads = "uploads"
        if os.path.isdir(uploads):
            files = [f for f in os.listdir(uploads)
                     if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                     and not f.startswith('processed_')]
            if files:
                img_path = os.path.join(uploads, files[0])

    if not img_path:
        print("  ⚠️  No test image found — skipping OCR scan test")
        return None

    print(f"  Using image: {img_path}")
    with open(img_path, "rb") as f:
        r = requests.post(f"{BASE}/api/receipts/scan",
                          files={"file": (os.path.basename(img_path), f, "image/jpeg")},
                          timeout=120)

    check("POST /api/scan (200)", r.status_code == 200)
    data = r.json()
    check("Response has 'success'", "success" in data, str(list(data.keys())))
    check("Scan succeeded", data.get("success") == True, data.get("error", ""))

    if data.get("success"):
        receipt_data = data.get("receipt_data", {})
        # Scan response uses db_id (int) for database ID, receipt_id is the string like RCP-...
        receipt_id = receipt_data.get("db_id") or receipt_data.get("id") or data.get("receipt_id")
        items = receipt_data.get("items", data.get("items", []))
        check("Has receipt_id", receipt_id is not None, f"receipt_data keys={list(receipt_data.keys())}")
        check("Has items", len(items) >= 1, f"got {len(items)} items")
        if items:
            # Scan response items use 'code' key, DB items use 'product_code'
            has_code = "code" in items[0] or "product_code" in items[0]
            check("Items have product_code", has_code, f"item keys={list(items[0].keys())}")
            check("Items have quantity", "quantity" in items[0])
            print(f"  📝 Extracted {len(items)} items:")
            for it in items:
                code = it.get('code', it.get('product_code', '?'))
                print(f"     {code} x {it.get('quantity', '?')} -- {it.get('product_name', 'unknown')}")
        return receipt_id
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8: View Receipt
# ═══════════════════════════════════════════════════════════════════════════════
def test_8_view_receipt(receipt_id):
    print("\n═══ Test 8: View Receipt ═══")
    if not receipt_id:
        check("Skipped — no receipt_id", False)
        return
    r = requests.get(f"{BASE}/api/receipts/{receipt_id}")
    check("GET /api/receipts/{id} (200)", r.status_code == 200)
    data = r.json()
    check("Receipt has items", "items" in data and len(data["items"]) >= 1)
    check("Receipt has receipt_number", "receipt_number" in data)
    check("Receipt has created_at", "created_at" in data)
    print(f"  Receipt #{receipt_id}: {len(data.get('items', []))} items, receipt_number={data.get('receipt_number', 'N/A')}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9: Update Item (edit quantity)
# ═══════════════════════════════════════════════════════════════════════════════
def test_9_update_item(receipt_id):
    print("\n═══ Test 9: Update Item (edit quantity) ═══")
    if not receipt_id:
        check("Skipped — no receipt_id", False)
        return
    r = requests.get(f"{BASE}/api/receipts/{receipt_id}")
    items = r.json().get("items", [])
    if not items:
        check("Skipped — no items", False)
        return

    item = items[0]
    item_id = item["id"]
    original_qty = item["quantity"]
    new_qty = original_qty + 5

    r2 = requests.put(f"{BASE}/api/receipts/items/{item_id}", json={
        "product_code": item["product_code"],
        "product_name": item.get("product_name", "Test"),
        "quantity": new_qty
    })
    check("PUT /api/receipts/items/{id} (200)", r2.status_code == 200)

    # Verify update
    r3 = requests.get(f"{BASE}/api/receipts/{receipt_id}")
    updated_item = [i for i in r3.json()["items"] if i["id"] == item_id][0]
    check("Quantity updated correctly", updated_item["quantity"] == new_qty,
          f"expected {new_qty}, got {updated_item['quantity']}")

    # Restore original
    requests.put(f"{BASE}/api/receipts/items/{item_id}", json={
        "product_code": item["product_code"],
        "product_name": item.get("product_name", "Test"),
        "quantity": original_qty
    })
    print(f"  Updated item #{item_id}: qty {original_qty} -> {new_qty} -> {original_qty} (restored)")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 10: Add Row (new item) to Receipt
# ═══════════════════════════════════════════════════════════════════════════════
def test_10_add_row(receipt_id):
    print("\n═══ Test 10: Add Row (new item) to Receipt ═══")
    if not receipt_id:
        check("Skipped — no receipt_id", False)
        return

    # Count items before
    r = requests.get(f"{BASE}/api/receipts/{receipt_id}")
    items_before = len(r.json().get("items", []))

    payload = {"product_code": "XYZ", "product_name": "Wall Filler 500g", "quantity": 7}
    r2 = requests.post(f"{BASE}/api/receipts/{receipt_id}/items", json=payload)
    check("POST /api/receipts/{id}/items (200)", r2.status_code == 200)
    data = r2.json()
    check("Response has item_id", "item_id" in data, str(data))

    # Verify it appears
    r3 = requests.get(f"{BASE}/api/receipts/{receipt_id}")
    items_after = r3.json().get("items", [])
    check("Item count increased", len(items_after) == items_before + 1,
          f"before={items_before}, after={len(items_after)}")
    codes = [i["product_code"] for i in items_after]
    check("XYZ now in receipt items", "XYZ" in codes, str(codes))
    print(f"  Added XYZ x 7 to receipt #{receipt_id}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 11: Receipts List & Date Filter
# ═══════════════════════════════════════════════════════════════════════════════
def test_11_receipts_list():
    print("\n═══ Test 11: Receipts List & Date Filter ═══")
    r = requests.get(f"{BASE}/api/receipts")
    check("GET /api/receipts (200)", r.status_code == 200)
    data = r.json()
    check("Response has 'receipts' key", "receipts" in data)
    receipts = data.get("receipts", [])
    check("At least 1 receipt exists", len(receipts) >= 1, f"got {len(receipts)}")
    print(f"  Total receipts: {len(receipts)}")

    # Date filter
    today = time.strftime("%Y-%m-%d")
    r2 = requests.get(f"{BASE}/api/receipts/date/{today}")
    check(f"GET /api/receipts/date/{today} (200)", r2.status_code == 200)
    dated = r2.json()
    check("Date response has 'receipts'", "receipts" in dated)
    check("Date response has 'count'", "count" in dated)
    print(f"  Today's receipts: {dated.get('count', 0)}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 12: Export Excel
# ═══════════════════════════════════════════════════════════════════════════════
def test_12_export_excel(receipt_id):
    print("\n═══ Test 12: Export Excel ═══")
    if not receipt_id:
        check("Skipped — no receipt_id", False)
        return
    r = requests.post(f"{BASE}/api/export/excel", json={"receipt_ids": [receipt_id]})
    check("POST /api/export/excel (200)", r.status_code == 200)
    data = r.json()
    check("Export has file_path", "file_path" in data, str(data.keys()))
    check("Export has download_url", "download_url" in data)
    if "download_url" in data:
        r2 = requests.get(f"{BASE}{data['download_url']}")
        check("Download file works (200)", r2.status_code == 200)
        check("File has content", len(r2.content) > 100)
        print(f"  Exported: {data.get('file_path', 'N/A')}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 13: Daily Report
# ═══════════════════════════════════════════════════════════════════════════════
def test_13_daily_report(receipt_id):
    print("\n═══ Test 13: Daily Report ═══")
    today = time.strftime("%Y-%m-%d")
    r = requests.get(f"{BASE}/api/export/daily", params={"date": today})
    if receipt_id:
        # We scanned a receipt today, so we should have data
        check("GET /api/export/daily (200)", r.status_code == 200)
        data = r.json()
        check("Daily report has file_path", "file_path" in data, str(data.keys()))
        if "download_url" in data:
            r2 = requests.get(f"{BASE}{data['download_url']}")
            check("Daily report downloadable", r2.status_code == 200)
            print(f"  Daily report: {data.get('file_path', 'N/A')}")
    else:
        # No receipts today — expect 400 with proper error message
        check("Daily report returns 400 when no receipts", r.status_code == 400)
        data = r.json()
        check("Error has detail message", "detail" in data)

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 14: Dashboard
# ═══════════════════════════════════════════════════════════════════════════════
def test_14_dashboard():
    print("\n═══ Test 14: Dashboard Stats ═══")
    r = requests.get(f"{BASE}/api/dashboard")
    check("GET /api/dashboard (200)", r.status_code == 200)
    data = r.json()
    check("Has 'today' stats", "today" in data)
    check("Has 'total_products'", "total_products" in data)
    check("Has 'recent_receipts'", "recent_receipts" in data)
    today = data.get("today", {})
    print(f"  Today: {today.get('receipts_count', 0)} receipts, {today.get('items_count', 0)} items")
    print(f"  Total products: {data.get('total_products', 0)}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 15: Delete Receipt
# ═══════════════════════════════════════════════════════════════════════════════
def test_15_delete_receipt(receipt_id):
    print("\n═══ Test 15: Delete Receipt ═══")
    if not receipt_id:
        check("Skipped — no receipt_id", False)
        return
    r = requests.delete(f"{BASE}/api/receipts/{receipt_id}")
    check("DELETE /api/receipts/{id} (200)", r.status_code == 200)

    r2 = requests.get(f"{BASE}/api/receipts/{receipt_id}")
    check("Receipt returns 404 after delete", r2.status_code == 404)
    print(f"  Deleted receipt #{receipt_id}")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST 16: Validation Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════
def test_16_validation():
    print("\n═══ Test 16: Validation Edge Cases ═══")
    r = requests.post(f"{BASE}/api/products", json={"product_code": "", "product_name": "Bad"})
    check("Empty code rejected (422)", r.status_code == 422, f"got {r.status_code}")

    r2 = requests.post(f"{BASE}/api/products", json={"product_code": "ABCDEFGHIJK", "product_name": "Bad"})
    check("Code >10 chars rejected (422)", r2.status_code == 422, f"got {r2.status_code}")

    r3 = requests.post(f"{BASE}/api/products", json={"product_code": "ZZZ", "product_name": ""})
    check("Empty name rejected (422)", r3.status_code == 422, f"got {r3.status_code}")

    r4 = requests.delete(f"{BASE}/api/receipts/99999")
    check("Delete non-existent receipt -> 404", r4.status_code == 404, f"got {r4.status_code}")

    r5 = requests.delete(f"{BASE}/api/products/ZZZZZ")
    check("Delete non-existent product -> 404", r5.status_code == 404, f"got {r5.status_code}")

    r6 = requests.get(f"{BASE}/api/products/ZZZZZ")
    check("Get non-existent product -> 404", r6.status_code == 404, f"got {r6.status_code}")

    r7 = requests.post(f"{BASE}/api/receipts/scan", files={"file": ("test.txt", b"hello", "text/plain")})
    check("Scan non-image rejected (400)", r7.status_code == 400, f"got {r7.status_code}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global PASS, FAIL

    print("=" * 60)
    print("  HANDWRITTEN RECEIPT SCANNER -- FULL E2E TEST SUITE")
    print("=" * 60)

    print("\nStarting test server on port 8765...")
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    if not wait_for_server():
        print("Server failed to start within 30s!")
        sys.exit(1)
    print("Server is ready!\n")

    try:
        test_1_homepage_and_static()
        test_2_products_catalog()
        test_3_add_product()
        test_4_edit_product()
        test_5_search_products()
        test_6_delete_product()
        receipt_id = test_7_scan_receipt()
        # Fallback: if no image was available, use an existing receipt
        if not receipt_id:
            r = requests.get(f"{BASE}/api/receipts")
            existing = r.json().get("receipts", [])
            if existing:
                receipt_id = existing[0]["id"]
                print(f"  Using existing receipt #{receipt_id} for remaining tests")
        test_8_view_receipt(receipt_id)
        test_9_update_item(receipt_id)
        test_10_add_row(receipt_id)
        test_11_receipts_list()
        test_12_export_excel(receipt_id)
        test_13_daily_report(receipt_id)
        test_14_dashboard()
        test_15_delete_receipt(receipt_id)
        test_16_validation()

    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()

    total = PASS + FAIL
    print("\n" + "=" * 60)
    if FAIL == 0:
        print(f"  ALL {PASS} TESTS PASSED!")
    else:
        print(f"  RESULTS: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 60)
    if ERRORS:
        print("\n  FAILURES:")
        for e in ERRORS:
            print(f"    {e}")
    print()

    sys.exit(1 if FAIL > 0 else 0)

if __name__ == "__main__":
    main()
