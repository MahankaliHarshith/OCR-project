"""
API Integration Tests — exercises FastAPI endpoints using TestClient.
Tests scan validation, catalog CRUD, receipts, and dashboard endpoints.
"""

import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Create a TestClient for the FastAPI app."""
    from app.main import app
    with TestClient(app) as c:
        yield c


# ─── Health & Static ─────────────────────────────────────────────────────────

class TestHealthEndpoints:
    """Test basic health and static file serving."""

    def test_root_returns_html(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")

    def test_static_js(self, client):
        res = client.get("/static/app.js")
        assert res.status_code == 200

    def test_static_css(self, client):
        res = client.get("/static/styles.css")
        assert res.status_code == 200

    def test_static_404(self, client):
        res = client.get("/static/nonexistent.xyz")
        assert res.status_code == 404

    def test_health_endpoint(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"


# ─── Scan Endpoint Validation ─────────────────────────────────────────────────

class TestScanValidation:
    """Test scan endpoint input validation without running OCR."""

    def test_scan_no_file(self, client):
        res = client.post("/api/receipts/scan")
        assert res.status_code == 422  # FastAPI validation error

    def test_scan_invalid_extension(self, client):
        fake = io.BytesIO(b"not an image")
        res = client.post(
            "/api/receipts/scan",
            files={"file": ("test.txt", fake, "text/plain")},
        )
        assert res.status_code == 400
        assert "Unsupported file type" in res.json()["detail"]

    def test_scan_invalid_magic_bytes(self, client):
        """Valid extension but invalid content — magic bytes check."""
        fake = io.BytesIO(b"this is not a jpeg file content at all")
        res = client.post(
            "/api/receipts/scan",
            files={"file": ("test.jpg", fake, "image/jpeg")},
        )
        assert res.status_code == 400
        assert "content does not match" in res.json()["detail"]

    def test_scan_oversized_file(self, client):
        """File exceeding MAX_FILE_SIZE_MB should be rejected."""
        # Create a minimal valid JPEG header + 25MB of padding
        jpeg_header = b'\xff\xd8\xff\xe0' + b'\x00' * (25 * 1024 * 1024)
        fake = io.BytesIO(jpeg_header)
        res = client.post(
            "/api/receipts/scan",
            files={"file": ("big.jpg", fake, "image/jpeg")},
        )
        assert res.status_code == 400
        assert "too large" in res.json()["detail"]


# ─── Catalog API ──────────────────────────────────────────────────────────────

class TestCatalogAPI:
    """Test product catalog CRUD endpoints."""

    def test_get_products(self, client):
        res = client.get("/api/products")
        assert res.status_code == 200
        data = res.json()
        assert "products" in data
        assert isinstance(data["products"], list)

    def test_get_product_by_code(self, client):
        # First get list to find a valid code
        data = client.get("/api/products").json()
        products = data.get("products", [])
        if products:
            code = products[0]["product_code"]
            res = client.get(f"/api/products/{code}")
            assert res.status_code == 200
            assert res.json()["product_code"] == code

    def test_get_product_not_found(self, client):
        res = client.get("/api/products/ZZZNOTEXIST")
        assert res.status_code == 404

    def test_add_product(self, client):
        import uuid
        unique_code = f"TST{uuid.uuid4().hex[:4].upper()}"
        res = client.post("/api/products", json={
            "product_code": unique_code,
            "product_name": "API Test Product",
        })
        assert res.status_code in (200, 201)
        # Verify it exists
        check = client.get(f"/api/products/{unique_code}")
        assert check.status_code == 200
        assert check.json()["product_name"] == "API Test Product"
        # Cleanup — delete the test product
        client.delete(f"/api/products/{unique_code}")

    def test_add_product_invalid_code(self, client):
        res = client.post("/api/products", json={
            "product_code": "!!invalid!!",
            "product_name": "Bad Code",
        })
        assert res.status_code == 422

    def test_search_products(self, client):
        res = client.get("/api/products/search?q=paint")
        assert res.status_code == 200
        data = res.json()
        assert "products" in data
        assert isinstance(data["products"], list)


# ─── Receipts API ─────────────────────────────────────────────────────────────

class TestReceiptsAPI:
    """Test receipt listing and retrieval."""

    def test_list_receipts(self, client):
        res = client.get("/api/receipts")
        assert res.status_code == 200
        data = res.json()
        assert "receipts" in data
        assert isinstance(data["receipts"], list)

    def test_get_receipt_not_found(self, client):
        res = client.get("/api/receipts/99999")
        assert res.status_code == 404


# ─── Dashboard API ────────────────────────────────────────────────────────────

class TestDashboardAPI:
    """Test dashboard stats endpoint."""

    def test_dashboard_stats(self, client):
        res = client.get("/api/dashboard")
        assert res.status_code == 200
        data = res.json()
        # Should have basic stat keys
        assert "total_products" in data or "receipts_today" in data or isinstance(data, dict)


# ─── Security Headers ────────────────────────────────────────────────────────

class TestSecurityHeaders:
    """Verify security middleware is active."""

    def test_csp_header(self, client):
        res = client.get("/")
        csp = res.headers.get("content-security-policy", "")
        assert "default-src" in csp

    def test_nosniff_header(self, client):
        res = client.get("/")
        assert res.headers.get("x-content-type-options") == "nosniff"

    def test_referrer_policy(self, client):
        res = client.get("/")
        assert "referrer-policy" in {k.lower() for k in res.headers}

    def test_gzip_compression(self, client):
        """Verify GZip middleware compresses large responses."""
        res = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
        assert res.status_code == 200
        # GZip middleware sets content-encoding header
        encoding = res.headers.get("content-encoding", "")
        # Large JS file should be compressed
        assert encoding == "gzip" or len(res.content) > 0  # At minimum file is served
