"""Quick health check of all API endpoints."""
import sys

import requests

base = 'http://localhost:8000'
ok = 0
fail = 0

tests = [
    ("GET", "/api/health", None),
    ("GET", "/api/dashboard", None),
    ("GET", "/api/products", None),
    ("GET", "/api/products/search?q=paint", None),
    ("GET", "/api/products/ABC", None),
    ("GET", "/api/receipts?limit=5", None),
    ("GET", "/api/receipts/date/2026-03-03", None),
    ("GET", "/api/ocr/status", None),
    ("GET", "/api/ocr/usage", None),
    ("GET", "/static/app.js", None),
    ("GET", "/static/styles.css", None),
    ("GET", "/", None),
    ("GET", "/api/nonexistent", 404),
]

for method, path, expect_code in tests:
    try:
        r = requests.request(method, f"{base}{path}", timeout=5)
        code = r.status_code
        expected = expect_code or 200
        status = "✅" if code == expected else "❌"
        if code != expected:
            fail += 1
        else:
            ok += 1
        # Response summary
        try:
            body = r.json()
            summary = str(body)[:80]
        except Exception:
            summary = f"{len(r.content)} bytes"
        print(f"{status} {method} {path} → {code} | {summary}")
    except Exception as e:
        fail += 1
        print(f"❌ {method} {path} → ERROR: {e}")

# Header check
try:
    r = requests.get(f"{base}/", timeout=5)
    h = dict(r.headers)
    xfo = h.get("x-frame-options", "NOT SET")
    xcto = h.get("x-content-type-options", "NOT SET")
    print(f"\n📋 Security Headers: X-Frame-Options={xfo}, X-Content-Type-Options={xcto}")
except Exception as e:
    print(f"📋 Headers check failed: {e}")

print(f"\n{'='*50}")
print(f"Results: {ok} passed, {fail} failed out of {ok+fail} tests")
if fail > 0:
    sys.exit(1)
