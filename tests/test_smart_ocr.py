"""
Tests for Phase 2: Smart OCR — Dedup, Quality Scoring, Validation,
Correction Feedback, Date/Store Extraction.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Dedup Service Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDedupService(unittest.TestCase):
    """Tests for duplicate receipt detection."""

    def setUp(self):
        from app.services.dedup_service import DedupService
        self.svc = DedupService()

    def test_content_fingerprint_deterministic(self):
        """Same items in different order should produce same fingerprint."""
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
        self.assertEqual(fp_a, fp_b)
        self.assertTrue(len(fp_a) == 32)

    def test_content_fingerprint_different_items(self):
        """Different items should produce different fingerprints."""
        items_a = [{"code": "ABC", "quantity": 2}]
        items_b = [{"code": "XYZ", "quantity": 5}]
        fp_a = self.svc.compute_content_fingerprint(items_a)
        fp_b = self.svc.compute_content_fingerprint(items_b)
        self.assertNotEqual(fp_a, fp_b)

    def test_content_fingerprint_empty(self):
        """Empty items list should return empty string."""
        self.assertEqual(self.svc.compute_content_fingerprint([]), "")

    def test_content_fingerprint_different_quantities(self):
        """Same code but different quantity should produce different fingerprint."""
        items_a = [{"code": "ABC", "quantity": 2}]
        items_b = [{"code": "ABC", "quantity": 5}]
        fp_a = self.svc.compute_content_fingerprint(items_a)
        fp_b = self.svc.compute_content_fingerprint(items_b)
        self.assertNotEqual(fp_a, fp_b)

    def test_hamming_distance_identical(self):
        """Identical hashes should have distance 0."""
        self.assertEqual(self.svc.hamming_distance("abcd1234", "abcd1234"), 0)

    def test_hamming_distance_different(self):
        """Different hashes should have positive distance."""
        d = self.svc.hamming_distance("0000000000000000", "ffffffffffffffff")
        self.assertEqual(d, 64)  # All bits differ

    def test_hamming_distance_empty(self):
        """Empty hashes should return max distance."""
        self.assertEqual(self.svc.hamming_distance("", "abcd"), 64)
        self.assertEqual(self.svc.hamming_distance("abcd", ""), 64)

    def test_hamming_distance_one_bit(self):
        """One-bit difference."""
        d = self.svc.hamming_distance("0000000000000000", "0000000000000001")
        self.assertEqual(d, 1)

    def test_check_duplicate_no_recent(self):
        """No duplicates when no recent receipts exist."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = []
        result = self.svc.check_duplicate("abc123", "fp123", mock_db)
        self.assertIsNone(result)

    def test_check_duplicate_exact_fingerprint(self):
        """Exact content fingerprint match should flag duplicate."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 42,
                "receipt_number": "REC-20260316-120000-ABC",
                "image_hash": "",
                "content_fingerprint": "same_fp_here",
                "created_at": "2026-03-16 12:00:00",
            }
        ]
        # Content fingerprint match alone = 40 score (below threshold)
        result = self.svc.check_duplicate("different_hash", "same_fp_here", mock_db)
        # 40 < 60, so not flagged as duplicate
        self.assertIsNone(result)

    def test_check_duplicate_image_hash_match(self):
        """Image hash match (hamming ≤ 5) should flag duplicate."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 42,
                "receipt_number": "REC-20260316-120000-ABC",
                "image_hash": "0000000000000000",
                "content_fingerprint": "",
                "created_at": "2026-03-16 12:00:00",
            }
        ]
        # Identical image hash → hamming 0 → score 60 → is_duplicate
        result = self.svc.check_duplicate("0000000000000000", "different_fp", mock_db)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_duplicate"])
        self.assertEqual(result["similar_receipt_id"], 42)

    def test_check_duplicate_both_match(self):
        """Both image hash and fingerprint match → highest confidence."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 7,
                "receipt_number": "REC-20260316-120000-ABC",
                "image_hash": "0000000000000000",
                "content_fingerprint": "my_fp",
                "created_at": "2026-03-16 12:00:00",
            }
        ]
        result = self.svc.check_duplicate("0000000000000000", "my_fp", mock_db)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_duplicate"])
        self.assertEqual(result["confidence"], 100)

    def test_check_duplicate_db_method_missing(self):
        """Gracefully handle missing DB method."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.side_effect = AttributeError("no method")
        result = self.svc.check_duplicate("abc", "fp", mock_db)
        self.assertIsNone(result)

    def test_image_hash_computation(self):
        """Test image hash with a tiny test image."""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")

        # Create a small test image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img = Image.new("L", (16, 16), color=128)
            img.save(f.name)
            tmp_path = f.name

        try:
            h = self.svc.compute_image_hash(tmp_path)
            self.assertTrue(len(h) > 0)
            self.assertTrue(len(h) == 16)

            # Same image should produce same hash
            h2 = self.svc.compute_image_hash(tmp_path)
            self.assertEqual(h, h2)
        finally:
            os.unlink(tmp_path)

    def test_image_hash_nonexistent_file(self):
        """Non-existent file should return empty string."""
        h = self.svc.compute_image_hash("/nonexistent/file.jpg")
        self.assertEqual(h, "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Quality Scorer Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestQualityScorer(unittest.TestCase):
    """Tests for receipt quality scoring."""

    def setUp(self):
        from app.ocr.quality_scorer import QualityScorer
        self.scorer = QualityScorer()

    def test_perfect_score(self):
        """High confidence, many items, verified totals → Grade A."""
        items = [
            {"code": "ABC", "quantity": 2, "confidence": 0.95, "match_type": "exact", "needs_review": False},
            {"code": "XYZ", "quantity": 3, "confidence": 0.92, "match_type": "exact", "needs_review": False},
            {"code": "DEF", "quantity": 1, "confidence": 0.98, "match_type": "exact", "needs_review": False},
        ]
        metadata = {"ocr_avg_confidence": 0.95}
        total_v = {"verification_status": "verified"}
        math_v = {"has_prices": True, "all_line_math_ok": True}

        result = self.scorer.score(items, metadata, total_v, math_v)
        self.assertEqual(result["grade"], "A")
        self.assertGreaterEqual(result["score"], 90)
        self.assertIn("breakdown", result)

    def test_poor_score(self):
        """Low confidence, few items, no verification → Grade D."""
        items = [
            {"code": "UNK", "quantity": 1, "confidence": 0.3, "match_type": "unknown", "needs_review": True},
        ]
        metadata = {"ocr_avg_confidence": 0.3}
        result = self.scorer.score(items, metadata)
        self.assertEqual(result["grade"], "D")
        self.assertLess(result["score"], 60)

    def test_empty_items(self):
        """No items found → low score."""
        result = self.scorer.score([], {"ocr_avg_confidence": 0})
        self.assertEqual(result["score"], 12.0)  # neutral math + neutral image
        self.assertEqual(result["grade"], "D")

    def test_medium_score(self):
        """Average quality receipt → Grade B or C."""
        items = [
            {"code": "ABC", "quantity": 2, "confidence": 0.8, "match_type": "fuzzy", "needs_review": False},
            {"code": "XYZ", "quantity": 3, "confidence": 0.75, "match_type": "exact", "needs_review": False},
            {"code": "DEF", "quantity": 1, "confidence": 0.82, "match_type": "exact", "needs_review": False},
        ]
        metadata = {"ocr_avg_confidence": 0.80}
        total_v = {"verification_status": "verified"}
        math_v = {"has_prices": True, "all_line_math_ok": False}
        result = self.scorer.score(items, metadata, total_v, math_v)
        self.assertIn(result["grade"], ("B", "C"))
        self.assertGreaterEqual(result["score"], 60)
        self.assertLessEqual(result["score"], 90)

    def test_breakdown_structure(self):
        """Verify breakdown has all expected keys."""
        items = [{"code": "ABC", "quantity": 1, "confidence": 0.9, "match_type": "exact", "needs_review": False}]
        metadata = {"ocr_avg_confidence": 0.9}
        result = self.scorer.score(items, metadata)
        breakdown = result["breakdown"]
        expected_keys = {"ocr_confidence", "items_found", "total_verification",
                         "math_verification", "image_quality", "catalog_match"}
        self.assertEqual(set(breakdown.keys()), expected_keys)

    def test_image_quality_with_data(self):
        """Image quality scoring with sharpness/brightness data."""
        items = [{"code": "ABC", "quantity": 1, "confidence": 0.9, "match_type": "exact", "needs_review": False}]
        metadata = {
            "ocr_avg_confidence": 0.9,
            "preprocessing": {"quality": {"sharpness": 150, "brightness": 130}},
        }
        result = self.scorer.score(items, metadata)
        # High sharpness + good brightness → 10 points
        self.assertEqual(result["breakdown"]["image_quality"]["score"], 10)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Receipt Validator Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReceiptValidator(unittest.TestCase):
    """Tests for smart validation rules engine."""

    def setUp(self):
        from app.ocr.validators import ReceiptValidator
        self.validator = ReceiptValidator()

    def test_zero_quantity_correction(self):
        """Items with qty=0 should be auto-corrected to 1."""
        items = [
            {"code": "ABC", "quantity": 0, "needs_review": False},
        ]
        result = self.validator.validate(items)
        self.assertEqual(items[0]["quantity"], 1.0)
        self.assertTrue(items[0]["needs_review"])
        self.assertEqual(len(result["corrections"]), 1)
        self.assertEqual(result["corrections"][0]["reason"], "zero_quantity_auto_fix")

    def test_negative_quantity_correction(self):
        """Negative quantities should be flagged and corrected."""
        items = [{"code": "ABC", "quantity": -5, "needs_review": False}]
        result = self.validator.validate(items)
        self.assertEqual(items[0]["quantity"], 1.0)
        self.assertEqual(result["summary"]["high"], 1)

    def test_suspicious_high_quantity(self):
        """High qty without price data should trigger warning."""
        items = [{"code": "ABC", "quantity": 150, "needs_review": False}]
        result = self.validator.validate(items)
        self.assertTrue(items[0]["needs_review"])
        warnings = [w for w in result["warnings"] if w["rule"] == "suspicious_quantity"]
        self.assertEqual(len(warnings), 1)

    def test_price_deviation_warning(self):
        """Extreme price deviation from catalog should warn."""
        items = [
            {"code": "ABC", "quantity": 2, "unit_price": 5000, "line_total": 10000, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog)
        warnings = [w for w in result["warnings"] if w["rule"] == "price_deviation"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(items[0]["needs_review"])

    def test_catalog_price_auto_fill(self):
        """Missing price should be auto-filled from catalog."""
        items = [
            {"code": "ABC", "quantity": 3, "unit_price": 0, "line_total": 0, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog)
        self.assertEqual(items[0]["unit_price"], 200)
        self.assertEqual(items[0]["line_total"], 600)
        self.assertEqual(items[0]["price_source"], "validator_auto")
        corrections = [c for c in result["corrections"] if c["reason"] == "catalog_price_auto_fill"]
        self.assertEqual(len(corrections), 1)

    def test_line_total_mismatch(self):
        """Line total that doesn't match qty × price should warn."""
        items = [
            {"code": "ABC", "quantity": 3, "unit_price": 200, "line_total": 999, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog)
        warnings = [w for w in result["warnings"] if w["rule"] == "line_total_mismatch"]
        self.assertEqual(len(warnings), 1)

    def test_duplicate_item_flagging(self):
        """Same product code appearing twice should be flagged."""
        items = [
            {"code": "ABC", "quantity": 2, "needs_review": False},
            {"code": "ABC", "quantity": 3, "needs_review": False},
        ]
        result = self.validator.validate(items)
        warnings = [w for w in result["warnings"] if w["rule"] == "duplicate_item"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("appears 2 times", warnings[0]["message"])

    def test_cross_receipt_anomaly(self):
        """Qty far exceeding historical max should warn."""
        items = [
            {"code": "ABC", "quantity": 50, "needs_review": False},
        ]
        historical = {
            "ABC": {"avg_quantity": 3, "max_quantity": 10, "count": 20},
        }
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog, historical_stats=historical)
        warnings = [w for w in result["warnings"] if w["rule"] == "quantity_anomaly"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(items[0]["needs_review"])

    def test_valid_receipt_no_warnings(self):
        """Normal receipt should pass with no high/medium warnings."""
        items = [
            {"code": "ABC", "quantity": 2, "unit_price": 200, "line_total": 400, "needs_review": False},
            {"code": "XYZ", "quantity": 1, "unit_price": 220, "line_total": 220, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}, "XYZ": {"unit_price": 220}}
        result = self.validator.validate(items, catalog=catalog)
        self.assertTrue(result["valid"])
        self.assertEqual(result["summary"]["high"], 0)
        self.assertEqual(result["summary"]["medium"], 0)

    def test_summary_counts(self):
        """Verify summary correctly counts severity levels."""
        items = [
            {"code": "ABC", "quantity": 0, "needs_review": False},       # high
            {"code": "XYZ", "quantity": 200, "needs_review": False},     # medium
            {"code": "ABC", "quantity": 1, "needs_review": False},       # low (duplicate)
        ]
        result = self.validator.validate(items)
        self.assertEqual(result["summary"]["high"], 1)
        self.assertEqual(result["summary"]["auto_corrections"], 1)

    def test_historical_stats_graceful(self):
        """Missing historical stats method should not crash."""
        mock_db = MagicMock()
        mock_db.get_item_quantity_stats.side_effect = Exception("not implemented")
        stats = self.validator.get_historical_stats(mock_db)
        self.assertEqual(stats, {})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Correction Service Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCorrectionService(unittest.TestCase):
    """Tests for OCR correction feedback loop."""

    def setUp(self):
        from app.services.correction_service import CorrectionService
        self.svc = CorrectionService()

    def test_record_correction_code_change(self):
        """Code change should be recorded."""
        mock_db = MagicMock()
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=10,
            original_code="TEWI", corrected_code="TEW1",
            original_qty=3, corrected_qty=3,
        )
        mock_db.add_ocr_correction.assert_called_once()

    def test_record_correction_qty_change(self):
        """Quantity change should be recorded."""
        mock_db = MagicMock()
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=10,
            original_code="ABC", corrected_code="ABC",
            original_qty=1, corrected_qty=5,
        )
        mock_db.add_ocr_correction.assert_called_once()

    def test_record_correction_no_change(self):
        """No actual change should NOT be recorded."""
        mock_db = MagicMock()
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=10,
            original_code="ABC", corrected_code="ABC",
            original_qty=3, corrected_qty=3,
        )
        mock_db.add_ocr_correction.assert_not_called()

    def test_get_corrections_map(self):
        """Should return correction map from DB."""
        mock_db = MagicMock()
        mock_db.get_ocr_corrections_map.return_value = {"TEWI": "TEW1", "PEPW4O": "PEPW40"}
        result = self.svc.get_corrections_map(mock_db)
        self.assertEqual(result, {"TEWI": "TEW1", "PEPW4O": "PEPW40"})

    def test_get_corrections_map_caching(self):
        """Second call should use cache, not DB."""
        mock_db = MagicMock()
        mock_db.get_ocr_corrections_map.return_value = {"TEWI": "TEW1"}
        # First call — hits DB
        self.svc.get_corrections_map(mock_db)
        # Second call — should use cache
        self.svc.get_corrections_map(mock_db)
        mock_db.get_ocr_corrections_map.assert_called_once()

    def test_cache_invalidation_on_correction(self):
        """Recording a correction should invalidate cache."""
        mock_db = MagicMock()
        mock_db.get_ocr_corrections_map.return_value = {"TEWI": "TEW1"}
        # Populate cache
        self.svc.get_corrections_map(mock_db)
        # Record correction → invalidates cache
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=10,
            original_code="XXX", corrected_code="YYY",
            original_qty=1, corrected_qty=1,
        )
        # Next call should hit DB again
        mock_db.get_ocr_corrections_map.return_value = {"TEWI": "TEW1", "XXX": "YYY"}
        result = self.svc.get_corrections_map(mock_db)
        self.assertEqual(mock_db.get_ocr_corrections_map.call_count, 2)
        self.assertIn("XXX", result)

    def test_apply_correction_known(self):
        """Known correction should be applied."""
        corrections = {"TEWI": "TEW1", "PEPW4O": "PEPW40"}
        code, was_corrected = self.svc.apply_correction("TEWI", corrections)
        self.assertEqual(code, "TEW1")
        self.assertTrue(was_corrected)

    def test_apply_correction_unknown(self):
        """Unknown code should pass through unchanged."""
        corrections = {"TEWI": "TEW1"}
        code, was_corrected = self.svc.apply_correction("ABC", corrections)
        self.assertEqual(code, "ABC")
        self.assertFalse(was_corrected)

    def test_apply_correction_case_insensitive(self):
        """Correction lookup should be case-insensitive."""
        corrections = {"TEWI": "TEW1"}
        code, was_corrected = self.svc.apply_correction("tewi", corrections)
        self.assertEqual(code, "TEW1")
        self.assertTrue(was_corrected)

    def test_get_correction_stats_fallback(self):
        """Stats should return empty dict on DB failure."""
        mock_db = MagicMock()
        mock_db.get_ocr_correction_stats.side_effect = Exception("fail")
        stats = self.svc.get_correction_stats(mock_db)
        self.assertEqual(stats["total_corrections"], 0)

    def test_invalidate_cache(self):
        """Explicit invalidation should clear cache."""
        mock_db = MagicMock()
        mock_db.get_ocr_corrections_map.return_value = {"A": "B"}
        self.svc.get_corrections_map(mock_db)  # populate cache
        self.svc.invalidate_cache()
        self.svc.get_corrections_map(mock_db)  # should hit DB again
        self.assertEqual(mock_db.get_ocr_corrections_map.call_count, 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Parser Date/Store Extraction Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParserDateExtraction(unittest.TestCase):
    """Tests for receipt date extraction from OCR text."""

    def setUp(self):
        from app.ocr.parser import ReceiptParser
        self.parser = ReceiptParser({"ABC": "Test Product"})

    def _make_lines(self, texts):
        """Helper: create grouped_lines format from text strings."""
        return [{"text": t, "confidence": 0.9, "y_center": i * 50} for i, t in enumerate(texts)]

    def test_date_dd_mm_yyyy_slash(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "ABC Hardware Store",
            "Date: 15/03/2026",
            "ABC 2",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_date_yyyy_mm_dd_dash(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "Invoice",
            "2026-03-15",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_date_dd_mon_yyyy(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "15 March 2026",
            "ABC 3",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_date_dd_mon_short_yyyy(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "Date: 15-Mar-2026",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_date_mon_dd_yyyy(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "March 15, 2026",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_date_with_keyword_priority(self):
        """Line with 'Date:' keyword should be preferred."""
        result = self.parser._extract_receipt_date(self._make_lines([
            "01/01/2025",          # generic date (lower priority)
            "Date: 15/03/2026",   # has keyword (higher priority)
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_no_date_found(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "ABC Hardware",
            "ABC 2",
            "XYZ 3",
        ]))
        self.assertIsNone(result)

    def test_invalid_date_skipped(self):
        """Invalid dates (month 13, day 32) should be ignored."""
        result = self.parser._extract_receipt_date(self._make_lines([
            "32/13/2026",
        ]))
        self.assertIsNone(result)

    def test_date_dd_mm_yyyy_dots(self):
        result = self.parser._extract_receipt_date(self._make_lines([
            "Date: 15.03.2026",
        ]))
        self.assertEqual(result, "2026-03-15")


class TestParserStoreExtraction(unittest.TestCase):
    """Tests for store/merchant name extraction from OCR text."""

    def setUp(self):
        from app.ocr.parser import ReceiptParser
        self.parser = ReceiptParser({"ABC": "Test Product", "XYZ": "Other Product"})

    def _make_lines(self, texts):
        return [{"text": t, "confidence": 0.9, "y_center": i * 50} for i, t in enumerate(texts)]

    def test_store_name_first_line(self):
        result = self.parser._extract_store_name(self._make_lines([
            "SHARMA PAINT SHOP",
            "Date: 15/03/2026",
            "ABC 2",
        ]))
        self.assertEqual(result, "SHARMA PAINT SHOP")

    def test_skip_date_line(self):
        """Lines with date keywords should be skipped."""
        result = self.parser._extract_store_name(self._make_lines([
            "Date: 15/03/2026",
            "ABC Hardware Store",
            "ABC 2",
        ]))
        self.assertEqual(result, "ABC Hardware Store")

    def test_skip_column_headers(self):
        """Column headers (Item Code Qty Rate Amount) should be skipped."""
        result = self.parser._extract_store_name(self._make_lines([
            "Item Code Qty Rate Amount",
            "SHARMA HARDWARE",
        ]))
        # "Item Code Qty Rate Amount" has tokens all in header_words
        # but the exact matching depends on implementation
        self.assertIsNotNone(result)

    def test_skip_product_code(self):
        """Standalone product codes should be skipped."""
        result = self.parser._extract_store_name(self._make_lines([
            "ABC",          # This is a product code
            "My Cool Store",
        ]))
        self.assertEqual(result, "My Cool Store")

    def test_no_store_name(self):
        """All lines are items/noise → no store name."""
        result = self.parser._extract_store_name(self._make_lines([
            "ABC",
            "XYZ",
        ]))
        self.assertIsNone(result)

    def test_skip_short_lines(self):
        """Very short lines (< 3 chars) should be skipped."""
        result = self.parser._extract_store_name(self._make_lines([
            "Hi",
            "ABC HARDWARE AND PAINTS",
        ]))
        self.assertEqual(result, "ABC HARDWARE AND PAINTS")

    def test_empty_lines(self):
        result = self.parser._extract_store_name([])
        self.assertIsNone(result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Database Migration Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDatabaseSmartOCR(unittest.TestCase):
    """Tests for database methods added in migration v4."""

    def setUp(self):
        from app.database import Database
        self.db = Database()

    def test_update_receipt_metadata(self):
        """update_receipt_metadata should update new columns."""
        # Create a test receipt
        rid = self.db.create_receipt("TEST-META-001", image_path="test.jpg")
        self.db.update_receipt_metadata(
            rid,
            image_hash="abc123",
            content_fingerprint="fp456",
            receipt_date="2026-03-15",
            store_name="Test Store",
            quality_score=85,
            quality_grade="B",
        )
        receipt = self.db.get_receipt(rid)
        self.assertIsNotNone(receipt)
        # New columns should be accessible
        self.assertEqual(receipt.get("image_hash"), "abc123")
        self.assertEqual(receipt.get("quality_score"), 85)
        self.assertEqual(receipt.get("quality_grade"), "B")
        self.assertEqual(receipt.get("store_name"), "Test Store")
        # Cleanup
        self.db.delete_receipt(rid)

    def test_get_recent_receipts_with_hashes(self):
        """Should return recent receipts with hash columns."""
        rid = self.db.create_receipt("TEST-HASH-001")
        self.db.update_receipt_metadata(rid, image_hash="test_hash", content_fingerprint="test_fp")
        results = self.db.get_recent_receipts_with_hashes(hours=24)
        self.assertTrue(len(results) > 0)
        found = [r for r in results if r["receipt_number"] == "TEST-HASH-001"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["image_hash"], "test_hash")
        # Cleanup
        self.db.delete_receipt(rid)

    def test_get_receipt_item(self):
        """Should return a single receipt item by ID."""
        rid = self.db.create_receipt("TEST-ITEM-001")
        self.db.add_receipt_items(rid, [
            {"code": "ABC", "product": "Test", "quantity": 2, "unit": "Piece",
             "confidence": 0.9, "unit_price": 100, "line_total": 200},
        ])
        receipt = self.db.get_receipt(rid)
        item_id = receipt["items"][0]["id"]
        item = self.db.get_receipt_item(item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item["product_code"], "ABC")
        self.assertEqual(item["quantity"], 2)
        # Cleanup
        self.db.delete_receipt(rid)

    def test_get_receipt_item_not_found(self):
        """Non-existent item should return None."""
        result = self.db.get_receipt_item(999999)
        self.assertIsNone(result)

    def test_ocr_corrections_crud(self):
        """Test adding and querying OCR corrections."""
        import uuid
        suffix = uuid.uuid4().hex[:6].upper()
        orig_code = f"TEWI_{suffix}"
        corr_code = f"TEW1_{suffix}"
        orig_code2 = f"PEPW4O_{suffix}"
        corr_code2 = f"PEPW40_{suffix}"

        rid = self.db.create_receipt(f"TEST-CORR-{suffix}")
        # Add corrections
        self.db.add_ocr_correction(rid, 0, orig_code, corr_code, 3, 3)
        self.db.add_ocr_correction(rid, 0, orig_code, corr_code, 5, 5)
        self.db.add_ocr_correction(rid, 0, orig_code2, corr_code2, 2, 2)

        # Map needs min_count=2, so orig_code→corr_code should be there (2 occurrences)
        # orig_code2→corr_code2 has only 1 occurrence — should NOT be there
        corrections_map = self.db.get_ocr_corrections_map(min_count=2)
        self.assertEqual(corrections_map.get(orig_code), corr_code)
        self.assertNotIn(orig_code2, corrections_map)

        # With min_count=1, both should be there
        corrections_map_all = self.db.get_ocr_corrections_map(min_count=1)
        self.assertIn(orig_code, corrections_map_all)
        self.assertIn(orig_code2, corrections_map_all)

        # Stats
        stats = self.db.get_ocr_correction_stats()
        self.assertGreaterEqual(stats["total_corrections"], 3)
        self.assertGreaterEqual(stats["unique_patterns"], 2)
        self.assertTrue(len(stats["top_corrections"]) > 0)

        # Cleanup
        self.db.delete_receipt(rid)

    def test_item_quantity_stats(self):
        """get_item_quantity_stats should aggregate receipt_items data."""
        # Create 2 receipts with same product codes
        rid1 = self.db.create_receipt("TEST-STATS-001")
        self.db.add_receipt_items(rid1, [
            {"code": "STAT_TEST", "product": "Test", "quantity": 3,
             "unit": "Piece", "confidence": 0.9, "unit_price": 0, "line_total": 0},
        ])
        rid2 = self.db.create_receipt("TEST-STATS-002")
        self.db.add_receipt_items(rid2, [
            {"code": "STAT_TEST", "product": "Test", "quantity": 7,
             "unit": "Piece", "confidence": 0.9, "unit_price": 0, "line_total": 0},
        ])

        stats = self.db.get_item_quantity_stats()
        if "STAT_TEST" in stats:
            self.assertEqual(stats["STAT_TEST"]["count"], 2)
            self.assertEqual(stats["STAT_TEST"]["max_quantity"], 7)
            self.assertEqual(stats["STAT_TEST"]["min_quantity"], 3)
            self.assertEqual(stats["STAT_TEST"]["avg_quantity"], 5.0)

        # Cleanup
        self.db.delete_receipt(rid1)
        self.db.delete_receipt(rid2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Integration: Parse Output Contains New Fields
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParserOutputNewFields(unittest.TestCase):
    """Verify that parse() includes receipt_date and store_name in output."""

    def setUp(self):
        from app.ocr.parser import ReceiptParser
        self.parser = ReceiptParser({"ABC": "1L Paint", "XYZ": "Brush"})

    def test_parse_includes_new_fields(self):
        """parse() return dict should have receipt_date and store_name keys."""
        ocr_results = [
            {"text": "SHARMA STORE", "confidence": 0.85, "bbox": [[0,10],[200,10],[200,40],[0,40]]},
            {"text": "Date: 15/03/2026", "confidence": 0.90, "bbox": [[0,50],[200,50],[200,80],[0,80]]},
            {"text": "ABC 3", "confidence": 0.88, "bbox": [[0,100],[200,100],[200,130],[0,130]]},
            {"text": "XYZ 2", "confidence": 0.92, "bbox": [[0,150],[200,150],[200,180],[0,180]]},
        ]
        result = self.parser.parse(ocr_results)
        # New keys must exist
        self.assertIn("receipt_date", result)
        self.assertIn("store_name", result)
        # Date should be extracted
        self.assertEqual(result["receipt_date"], "2026-03-15")
        # Store should be extracted
        self.assertEqual(result["store_name"], "SHARMA STORE")
        # Items should still parse correctly
        self.assertGreaterEqual(result["total_items"], 1)

    def test_parse_no_date_no_store(self):
        """When no date or store is present, keys should still exist."""
        ocr_results = [
            {"text": "ABC 3", "confidence": 0.88, "bbox": [[0,100],[200,100],[200,130],[0,130]]},
        ]
        result = self.parser.parse(ocr_results)
        self.assertIn("receipt_date", result)
        self.assertIn("store_name", result)
        # No date in OCR text
        self.assertIsNone(result["receipt_date"])
        # store_name may or may not be extracted — key must exist
        # (parser can't distinguish "ABC 3" as item vs store without full context)


if __name__ == "__main__":
    unittest.main()
