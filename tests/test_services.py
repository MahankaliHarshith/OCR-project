"""
Unit tests for service modules.

Covers:
  - CorrectionService (app/services/correction_service.py)
  - DedupService (app/services/dedup_service.py)
"""

from unittest.mock import MagicMock

from app.services.correction_service import CorrectionService
from app.services.dedup_service import DedupService

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CorrectionService Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCorrectionService:
    """Tests for CorrectionService."""

    def setup_method(self):
        self.svc = CorrectionService()
        self.db = MagicMock()

    # ── record_correction ─────────────────────────────────────────────────

    def test_record_correction_code_change(self):
        """Records when the product code was changed."""
        self.svc.record_correction(
            self.db,
            receipt_id=1,
            item_id=10,
            original_code="TEWI",
            corrected_code="TEW1",
            original_qty=2.0,
            corrected_qty=2.0,
        )
        self.db.add_ocr_correction.assert_called_once()

    def test_record_correction_qty_change(self):
        """Records when only the quantity changed."""
        self.svc.record_correction(
            self.db,
            receipt_id=1,
            item_id=10,
            original_code="ABC",
            corrected_code="ABC",
            original_qty=2.0,
            corrected_qty=5.0,
        )
        self.db.add_ocr_correction.assert_called_once()

    def test_record_correction_no_change_skipped(self):
        """No DB call when nothing actually changed."""
        self.svc.record_correction(
            self.db,
            receipt_id=1,
            item_id=10,
            original_code="ABC",
            corrected_code="abc",  # same after .upper()
            original_qty=2.0,
            corrected_qty=2.0,
        )
        self.db.add_ocr_correction.assert_not_called()

    def test_record_correction_tiny_qty_diff_skipped(self):
        """Quantities within 0.01 tolerance are considered equal."""
        self.svc.record_correction(
            self.db,
            receipt_id=1,
            item_id=10,
            original_code="ABC",
            corrected_code="ABC",
            original_qty=2.0,
            corrected_qty=2.005,
        )
        self.db.add_ocr_correction.assert_not_called()

    def test_record_correction_invalidates_cache(self):
        """Cache is cleared when a correction is recorded."""
        # Pre-populate cache
        self.svc._corrections_cache = {"OLD": "NEW"}
        self.svc.record_correction(
            self.db,
            receipt_id=1,
            item_id=10,
            original_code="OLD",
            corrected_code="NEW2",
            original_qty=1.0,
            corrected_qty=1.0,
        )
        assert self.svc._corrections_cache is None

    def test_record_correction_db_error_handled(self):
        """DB errors are caught and don't crash."""
        self.db.add_ocr_correction.side_effect = Exception("DB down")
        # Should not raise
        self.svc.record_correction(
            self.db,
            receipt_id=1,
            item_id=10,
            original_code="OLD",
            corrected_code="NEW",
            original_qty=1.0,
            corrected_qty=1.0,
        )

    # ── get_corrections_map ───────────────────────────────────────────────

    def test_get_corrections_map_from_db(self):
        """Fetches corrections from the DB when cache is empty."""
        self.db.get_ocr_corrections_map.return_value = {"TEWI": "TEW1"}
        result = self.svc.get_corrections_map(self.db)
        assert result == {"TEWI": "TEW1"}
        self.db.get_ocr_corrections_map.assert_called_once_with(min_count=2)

    def test_get_corrections_map_cached(self):
        """Returns cached value without hitting DB."""
        self.svc._corrections_cache = {"CACHED": "VALUE"}
        result = self.svc.get_corrections_map(self.db)
        assert result == {"CACHED": "VALUE"}
        self.db.get_ocr_corrections_map.assert_not_called()

    def test_get_corrections_map_db_error(self):
        """Returns empty dict on DB error."""
        self.db.get_ocr_corrections_map.side_effect = Exception("fail")
        result = self.svc.get_corrections_map(self.db)
        assert result == {}

    # ── apply_correction ──────────────────────────────────────────────────

    def test_apply_correction_found(self):
        """Known misread is corrected."""
        corrections = {"TEWI": "TEW1", "PEPW4O": "PEPW40"}
        code, was_corrected = self.svc.apply_correction("tewi", corrections)
        assert code == "TEW1"
        assert was_corrected is True

    def test_apply_correction_not_found(self):
        """Unknown code passes through unchanged (uppercased)."""
        code, was_corrected = self.svc.apply_correction("abc", {})
        assert code == "ABC"
        assert was_corrected is False

    def test_apply_correction_strips_whitespace(self):
        """Leading/trailing whitespace is stripped before lookup."""
        corrections = {"ABC": "XYZ"}
        code, was_corrected = self.svc.apply_correction("  abc  ", corrections)
        assert code == "XYZ"
        assert was_corrected is True

    # ── get_correction_stats ──────────────────────────────────────────────

    def test_get_correction_stats_success(self):
        """Returns stats from DB."""
        expected = {"total_corrections": 10, "unique_patterns": 3, "top_corrections": []}
        self.db.get_ocr_correction_stats.return_value = expected
        result = self.svc.get_correction_stats(self.db)
        assert result == expected

    def test_get_correction_stats_db_error(self):
        """Returns default stats on DB error."""
        self.db.get_ocr_correction_stats.side_effect = Exception("fail")
        result = self.svc.get_correction_stats(self.db)
        assert result["total_corrections"] == 0

    # ── invalidate_cache ──────────────────────────────────────────────────

    def test_invalidate_cache(self):
        """Manually clearing the cache works."""
        self.svc._corrections_cache = {"X": "Y"}
        self.svc.invalidate_cache()
        assert self.svc._corrections_cache is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DedupService Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDedupService:
    """Tests for DedupService."""

    def setup_method(self):
        self.svc = DedupService()
        self.db = MagicMock()

    # ── compute_content_fingerprint ───────────────────────────────────────

    def test_fingerprint_empty_items(self):
        """Empty list → empty fingerprint."""
        assert self.svc.compute_content_fingerprint([]) == ""

    def test_fingerprint_items_no_codes(self):
        """Items with no codes → empty fingerprint."""
        items = [{"quantity": 2}, {"code": "", "quantity": 3}]
        assert self.svc.compute_content_fingerprint(items) == ""

    def test_fingerprint_deterministic(self):
        """Same items → same fingerprint regardless of order."""
        items_a = [
            {"code": "ABC", "quantity": 2},
            {"code": "XYZ", "quantity": 3},
        ]
        items_b = [
            {"code": "XYZ", "quantity": 3},
            {"code": "ABC", "quantity": 2},
        ]
        fp_a = self.svc.compute_content_fingerprint(items_a)
        fp_b = self.svc.compute_content_fingerprint(items_b)
        assert fp_a == fp_b
        assert len(fp_a) == 32  # sha256 truncated to 32 hex chars

    def test_fingerprint_different_items(self):
        """Different items → different fingerprint."""
        items_a = [{"code": "ABC", "quantity": 2}]
        items_b = [{"code": "XYZ", "quantity": 2}]
        assert self.svc.compute_content_fingerprint(items_a) != self.svc.compute_content_fingerprint(items_b)

    def test_fingerprint_case_insensitive(self):
        """Codes are uppercased — 'abc' and 'ABC' produce same fingerprint."""
        items_a = [{"code": "abc", "quantity": 2}]
        items_b = [{"code": "ABC", "quantity": 2}]
        assert self.svc.compute_content_fingerprint(items_a) == self.svc.compute_content_fingerprint(items_b)

    # ── hamming_distance ──────────────────────────────────────────────────

    def test_hamming_identical(self):
        """Identical hashes → distance 0."""
        assert self.svc.hamming_distance("abcd1234", "abcd1234") == 0

    def test_hamming_different(self):
        """Different hashes → positive distance."""
        d = self.svc.hamming_distance("0000000000000000", "ffffffffffffffff")
        assert d == 64  # all bits different

    def test_hamming_empty_hash(self):
        """Empty hash → max distance (64)."""
        assert self.svc.hamming_distance("", "abcd") == 64
        assert self.svc.hamming_distance("abcd", "") == 64

    def test_hamming_invalid_hex(self):
        """Invalid hex → max distance."""
        assert self.svc.hamming_distance("zzzz", "xxxx") == 64

    def test_hamming_one_bit_difference(self):
        """Hashes differing by 1 bit → distance 1."""
        assert self.svc.hamming_distance("0", "1") == 1

    # ── check_duplicate ───────────────────────────────────────────────────

    def test_check_duplicate_no_recent(self):
        """No recent receipts → no duplicate."""
        self.db.get_recent_receipts_with_hashes.return_value = []
        result = self.svc.check_duplicate("abc123", "fp123", self.db)
        assert result is None

    def test_check_duplicate_db_error(self):
        """DB method unavailable → returns None (graceful skip)."""
        self.db.get_recent_receipts_with_hashes.side_effect = Exception("no method")
        result = self.svc.check_duplicate("abc", "fp", self.db)
        assert result is None

    def test_check_duplicate_image_match(self):
        """Matching image hash → duplicate detected."""
        self.db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 42,
                "receipt_number": "REC-001",
                "image_hash": "abcdef0123456789",  # identical hash
                "content_fingerprint": "",
                "created_at": "2026-03-17T10:00:00",
            }
        ]
        result = self.svc.check_duplicate("abcdef0123456789", "", self.db)
        assert result is not None
        assert result["is_duplicate"] is True
        assert result["similar_receipt_id"] == 42
        assert "image_similarity" in result["reasons"][0]

    def test_check_duplicate_content_match(self):
        """Matching content fingerprint alone → not enough (score 40 < 60)."""
        self.db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 42,
                "receipt_number": "REC-001",
                "image_hash": "",
                "content_fingerprint": "matching_fp",
                "created_at": "2026-03-17T10:00:00",
            }
        ]
        result = self.svc.check_duplicate("", "matching_fp", self.db)
        # Content-only match gives score=40, threshold is 60 → not flagged
        assert result is None

    def test_check_duplicate_both_match(self):
        """Image + content match → high confidence duplicate."""
        self.db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 42,
                "receipt_number": "REC-001",
                "image_hash": "abcdef0123456789",
                "content_fingerprint": "fp_match",
                "created_at": "2026-03-17T10:00:00",
            }
        ]
        result = self.svc.check_duplicate("abcdef0123456789", "fp_match", self.db)
        assert result is not None
        assert result["confidence"] == 100  # 60 (image) + 40 (content)
        assert "identical_items" in result["reasons"]

    def test_check_duplicate_no_match(self):
        """Completely different hashes → no duplicate."""
        self.db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 42,
                "receipt_number": "REC-001",
                "image_hash": "1111111111111111",
                "content_fingerprint": "different_fp",
                "created_at": "2026-03-17T10:00:00",
            }
        ]
        result = self.svc.check_duplicate("ffffffffffffffff", "my_fp", self.db)
        assert result is None

    # ── compute_image_hash ────────────────────────────────────────────────

    def test_image_hash_bad_path(self):
        """Non-existent file → empty string."""
        result = self.svc.compute_image_hash("/nonexistent/image.png")
        assert result == ""

    def test_image_hash_valid_image(self, tmp_path):
        """Valid image → 16-char hex hash."""
        from PIL import Image
        img = Image.new("L", (64, 64), color=128)
        path = tmp_path / "test.png"
        img.save(str(path))
        result = self.svc.compute_image_hash(str(path))
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_image_hash_deterministic(self, tmp_path):
        """Same image → same hash."""
        from PIL import Image
        img = Image.new("L", (64, 64), color=200)
        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        img.save(str(p1))
        img.save(str(p2))
        assert self.svc.compute_image_hash(str(p1)) == self.svc.compute_image_hash(str(p2))
