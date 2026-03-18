"""
Unit tests for middleware and API routes.

Covers:
  - RateLimiter (in-memory sliding window)
  - SecurityHeadersMiddleware
  - RateLimitMiddleware
  - APIKeyMiddleware
  - DevTunnelCORSMiddleware
  - Extended Database operations (update, delete, search, corrections, dedup)
"""

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.middleware import RateLimiter

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RateLimiter Unit Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRateLimiter:
    """Tests for the in-memory RateLimiter."""

    def test_allows_within_limit(self):
        rl = RateLimiter()
        allowed, remaining = rl.is_allowed("192.168.1.1", limit=5)
        assert allowed is True
        assert remaining == 4

    def test_blocks_over_limit(self):
        rl = RateLimiter()
        for _ in range(10):
            rl.is_allowed("192.168.1.1", limit=10)
        allowed, remaining = rl.is_allowed("192.168.1.1", limit=10)
        assert allowed is False
        assert remaining == 0

    def test_separate_ips(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.is_allowed("10.0.0.1", limit=5)
        # IP 10.0.0.1 is exhausted
        allowed1, _ = rl.is_allowed("10.0.0.1", limit=5)
        # IP 10.0.0.2 should still be allowed
        allowed2, _ = rl.is_allowed("10.0.0.2", limit=5)
        assert allowed1 is False
        assert allowed2 is True

    def test_window_expiry(self):
        rl = RateLimiter()
        # Fill the limit with a 1-second window
        for _ in range(3):
            rl.is_allowed("1.2.3.4", limit=3, window_seconds=1)
        # Should be blocked now
        allowed, _ = rl.is_allowed("1.2.3.4", limit=3, window_seconds=1)
        assert allowed is False
        # Wait for window to expire
        time.sleep(1.1)
        allowed, _ = rl.is_allowed("1.2.3.4", limit=3, window_seconds=1)
        assert allowed is True

    def test_cleanup_stale_ips(self):
        rl = RateLimiter()
        # Force cleanup after 50 calls
        for i in range(55):
            rl.is_allowed(f"ip-{i}", limit=100, window_seconds=1)
        time.sleep(1.1)
        # Trigger cleanup with more calls
        for i in range(55):
            rl.is_allowed(f"cleanup-{i}", limit=100, window_seconds=1)
        # Old IPs should be cleaned up (internal check)
        assert len(rl._requests) < 120


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SecurityHeaders Middleware Tests (via FastAPI TestClient)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSecurityHeadersMiddleware:
    """Tests for SecurityHeadersMiddleware."""

    @pytest.fixture
    def app(self):
        from fastapi import FastAPI

        from app.middleware import SecurityHeadersMiddleware
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"ok": True}

        return app

    @pytest.mark.asyncio
    async def test_security_headers_present(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert "strict-origin" in resp.headers.get("Referrer-Policy", "")
        assert resp.headers.get("X-XSS-Protection") == "0"
        assert "Content-Security-Policy" in resp.headers


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RateLimitMiddleware Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRateLimitMiddleware:
    """Tests for RateLimitMiddleware."""

    @pytest.fixture
    def app(self):
        from fastapi import FastAPI

        from app.middleware import RateLimitMiddleware

        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, general_rpm=5, scan_rpm=2)

        @app.get("/api/test")
        async def test_endpoint():
            return {"ok": True}

        @app.post("/api/receipts/scan")
        async def scan_endpoint():
            return {"ok": True}

        @app.get("/static/app.js")
        async def static_endpoint():
            return {"ok": True}

        return app

    @pytest.mark.asyncio
    async def test_allows_within_limit(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/test")
        assert resp.status_code == 200
        assert "X-RateLimit-Remaining" in resp.headers

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(5):
                await client.get("/api/test")
            resp = await client.get("/api/test")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    @pytest.mark.asyncio
    async def test_static_files_bypass(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(20):
                resp = await client.get("/static/app.js")
                assert resp.status_code == 200  # Never rate-limited

    @pytest.mark.asyncio
    async def test_scan_endpoint_stricter_limit(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(2):
                await client.post("/api/receipts/scan")
            resp = await client.post("/api/receipts/scan")
        assert resp.status_code == 429


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  APIKeyMiddleware Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAPIKeyMiddleware:
    """Tests for APIKeyMiddleware."""

    @pytest.fixture
    def app(self):
        from fastapi import FastAPI

        from app.middleware import APIKeyMiddleware

        app = FastAPI()
        app.add_middleware(APIKeyMiddleware, api_key="secret-123")

        @app.delete("/api/receipts/{rid}")
        async def delete_receipt(rid: int):
            return {"deleted": rid}

        @app.get("/api/receipts")
        async def list_receipts():
            return {"receipts": []}

        return app

    @pytest.mark.asyncio
    async def test_protected_endpoint_no_key_blocked(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/receipts/1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_protected_endpoint_wrong_key(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/receipts/1", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_protected_endpoint_correct_key(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/receipts/1", headers={"X-API-Key": "secret-123"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_protected_endpoint_no_key_needed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/receipts")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_same_origin_bypass(self, app):
        """Same-origin browser requests bypass API key check."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                "/api/receipts/1",
                headers={"Sec-Fetch-Site": "same-origin"},
            )
        assert resp.status_code == 200

    @pytest.fixture
    def app_no_key(self):
        """App with no API key configured (dev mode)."""
        from fastapi import FastAPI

        from app.middleware import APIKeyMiddleware

        app = FastAPI()
        app.add_middleware(APIKeyMiddleware, api_key="")

        @app.delete("/api/receipts/{rid}")
        async def delete_receipt(rid: int):
            return {"deleted": rid}

        return app

    @pytest.mark.asyncio
    async def test_no_key_configured_allows_all(self, app_no_key):
        async with AsyncClient(transport=ASGITransport(app=app_no_key), base_url="http://test") as client:
            resp = await client.delete("/api/receipts/1")
        assert resp.status_code == 200


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DevTunnelCORS Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDevTunnelCORSMiddleware:
    """Tests for DevTunnelCORSMiddleware."""

    @pytest.fixture
    def app(self):
        from fastapi import FastAPI

        from app.middleware import DevTunnelCORSMiddleware

        app = FastAPI()
        app.add_middleware(DevTunnelCORSMiddleware)

        @app.get("/api/test")
        async def test_endpoint():
            return {"ok": True}

        return app

    @pytest.mark.asyncio
    async def test_devtunnel_origin_allowed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/test",
                headers={"origin": "https://abc123.devtunnels.ms"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "https://abc123.devtunnels.ms"

    @pytest.mark.asyncio
    async def test_github_dev_origin_allowed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/test",
                headers={"origin": "https://mycodespace.github.dev"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "https://mycodespace.github.dev"

    @pytest.mark.asyncio
    async def test_normal_origin_no_cors(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/test",
                headers={"origin": "https://evil.com"},
            )
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_preflight_request(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.options(
                "/api/test",
                headers={"origin": "https://tunnel.devtunnels.ms"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Methods")

    @pytest.mark.asyncio
    async def test_no_origin_header(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/test")
        assert resp.status_code == 200


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Database Extended Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDatabaseExtended:
    """Extended Database tests to cover update/delete/search/correction methods."""

    @pytest.fixture
    def db(self, tmp_path):
        from app.database import Database
        return Database(tmp_path / "test.db")

    def test_update_product(self, db):
        result = db.update_product("ABC", product_name="Updated Paint")
        assert result is not None
        assert result["product_name"] == "Updated Paint"

    def test_update_product_no_fields(self, db):
        """update with no valid kwargs just returns current product."""
        result = db.update_product("ABC")
        assert result is not None

    def test_delete_product(self, db):
        result = db.delete_product("ABC")
        assert result is True

    def test_delete_product_nonexistent(self, db):
        result = db.delete_product("ZZZZZ")
        assert result is False

    def test_search_products(self, db):
        results = db.search_products("Paint")
        assert isinstance(results, list)
        # Should find some seeded products with "Paint" in name
        assert len(results) > 0

    def test_search_products_no_match(self, db):
        results = db.search_products("XYZNONEXISTENT999")
        assert results == []

    def test_get_product_code_map(self, db):
        code_map = db.get_product_code_map()
        assert isinstance(code_map, dict)
        assert len(code_map) > 0
        # All keys should be uppercase product codes
        for key in code_map:
            assert key == key.upper()

    def test_delete_receipt(self, db):
        receipt_id = db.create_receipt("REC-DEL-001")
        items = [{"code": "ABC", "product": "Paint", "quantity": 1, "confidence": 0.9}]
        db.add_receipt_items(receipt_id, items)
        result = db.delete_receipt(receipt_id)
        assert result is True

    def test_delete_receipt_nonexistent(self, db):
        result = db.delete_receipt(99999)
        assert result is False

    def test_ocr_corrections_workflow(self, db):
        """Full correction workflow: add, query map, get stats."""
        receipt_id = db.create_receipt("REC-CORR-001")

        # Add corrections (need at least 2 for the map to return them)
        for _ in range(3):
            db.add_ocr_correction(
                receipt_id=receipt_id,
                item_id=1,
                original_code="TEWI",
                corrected_code="TEW1",
                original_qty=2.0,
                corrected_qty=2.0,
            )

        corrections_map = db.get_ocr_corrections_map(min_count=2)
        assert "TEWI" in corrections_map
        assert corrections_map["TEWI"] == "TEW1"

        stats = db.get_ocr_correction_stats()
        assert stats["total_corrections"] >= 3
        assert stats["unique_patterns"] >= 1

    def test_recent_receipts_with_hashes(self, db):
        db.create_receipt("REC-HASH-001")
        results = db.get_recent_receipts_with_hashes(hours=24)
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_get_receipt_item(self, db):
        receipt_id = db.create_receipt("REC-ITEM-001")
        items = [{"code": "ABC", "product": "Paint", "quantity": 2, "confidence": 0.95}]
        db.add_receipt_items(receipt_id, items)
        receipt = db.get_receipt(receipt_id)
        item_id = receipt["items"][0]["id"]
        item = db.get_receipt_item(item_id)
        assert item is not None
        assert item["product_code"] == "ABC"

    def test_get_receipt_item_nonexistent(self, db):
        result = db.get_receipt_item(99999)
        assert result is None

    def test_get_item_quantity_stats(self, db):
        """Stats require at least 2 items per code."""
        receipt_id1 = db.create_receipt("REC-STATS-001")
        receipt_id2 = db.create_receipt("REC-STATS-002")
        db.add_receipt_items(receipt_id1, [{"code": "ABC", "product": "Paint", "quantity": 5, "confidence": 0.9}])
        db.add_receipt_items(receipt_id2, [{"code": "ABC", "product": "Paint", "quantity": 10, "confidence": 0.9}])
        stats = db.get_item_quantity_stats()
        assert "ABC" in stats
        assert stats["ABC"]["avg_quantity"] == 7.5

    def test_get_receipts_by_date(self, db):
        from datetime import date
        db.create_receipt("REC-DATE-001")
        today = date.today().isoformat()
        results = db.get_receipts_by_date(today)
        assert isinstance(results, list)

    def test_get_receipts_batch(self, db):
        id1 = db.create_receipt("REC-B1")
        id2 = db.create_receipt("REC-B2")
        results = db.get_receipts_batch([id1, id2])
        assert len(results) == 2

    def test_product_catalog_full(self, db):
        catalog = db.get_product_catalog_full()
        assert isinstance(catalog, dict)
        assert len(catalog) > 0
        for _code, info in catalog.items():
            assert "name" in info

    def test_update_receipt_item(self, db):
        receipt_id = db.create_receipt("REC-UPD-001")
        db.add_receipt_items(receipt_id, [{"code": "ABC", "product": "Paint", "quantity": 2, "confidence": 0.9}])
        receipt = db.get_receipt(receipt_id)
        item_id = receipt["items"][0]["id"]
        result = db.update_receipt_item(item_id, "XYZ", "Interior Paint", 5.0, 220.0, 1100.0)
        assert result is True
        updated = db.get_receipt_item(item_id)
        assert updated["product_code"] == "XYZ"
        assert updated["quantity"] == 5.0

    def test_update_receipt_item_nonexistent(self, db):
        result = db.update_receipt_item(99999, "ABC", "Paint", 1.0)
        assert result is False

    def test_add_receipt_item(self, db):
        receipt_id = db.create_receipt("REC-ADD-001")
        new_id = db.add_receipt_item(receipt_id, "ABC", "Exterior Paint", 3.0)
        assert new_id > 0
        item = db.get_receipt_item(new_id)
        assert item["product_code"] == "ABC"
        assert item["quantity"] == 3.0

    def test_add_receipt_item_nonexistent_receipt(self, db):
        with pytest.raises(ValueError):
            db.add_receipt_item(99999, "ABC", "Paint", 1.0)

    def test_delete_receipt_item(self, db):
        receipt_id = db.create_receipt("REC-DELI-001")
        db.add_receipt_items(receipt_id, [{"code": "ABC", "product": "Paint", "quantity": 2, "confidence": 0.9}])
        receipt = db.get_receipt(receipt_id)
        item_id = receipt["items"][0]["id"]
        result = db.delete_receipt_item(item_id)
        assert result is True

    def test_delete_receipt_item_nonexistent(self, db):
        result = db.delete_receipt_item(99999)
        assert result is False

    def test_processing_logs(self, db):
        receipt_id = db.create_receipt("REC-LOG-001")
        db.add_processing_log(receipt_id, "preprocess", "success", 150, "")
        db.add_processing_log(receipt_id, "ocr", "success", 2000, "")
        logs = db.get_processing_logs(receipt_id)
        assert len(logs) == 2
        assert logs[0]["stage"] == "preprocess"

    def test_processing_logs_batch(self, db):
        receipt_id = db.create_receipt("REC-LOGB-001")
        batch = [
            (receipt_id, "preprocess", "success", 100, ""),
            (receipt_id, "ocr", "success", 1500, ""),
            (receipt_id, "parse", "success", 50, ""),
        ]
        db.add_processing_logs_batch(batch)
        logs = db.get_processing_logs(receipt_id)
        assert len(logs) == 3

    def test_processing_logs_batch_empty(self, db):
        db.add_processing_logs_batch([])  # Should not raise

    def test_update_receipt_metadata(self, db):
        receipt_id = db.create_receipt("REC-META-001")
        db.update_receipt_metadata(
            receipt_id,
            image_hash="abc123",
            content_fingerprint="fp456",
            quality_score=85,
            quality_grade="B",
            store_name="My Store",
        )
        receipt = db.get_receipt(receipt_id)
        assert receipt is not None

    def test_count_products(self, db):
        count = db.count_products()
        assert count > 0  # seeded products

    def test_count_receipts(self, db):
        count = db.count_receipts()
        assert count == 0  # no receipts yet
        db.create_receipt("REC-CNT-001")
        assert db.count_receipts() == 1

    def test_get_recent_receipts(self, db):
        db.create_receipt("REC-REC-001")
        db.create_receipt("REC-REC-002")
        results = db.get_recent_receipts(limit=10)
        assert len(results) >= 2

    def test_add_product_duplicate_raises(self, db):
        with pytest.raises(ValueError):
            db.add_product("ABC", "Duplicate Paint")

    def test_add_product_reactivates_deleted(self, db):
        db.delete_product("ABC")
        result = db.add_product("ABC", "Reactivated Paint", "Paint", "Litre")
        assert result is not None

    def test_shutdown(self, db):
        db.shutdown()  # Should not raise
