"""
Deep edge-case & regression tests for Phase 2: Smart OCR.

Covers boundary conditions, type safety, concurrency, malformed input,
and real-world OCR quirks that the happy-path tests don't reach.
"""

import contextlib
import os
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Dedup Service — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDedupEdgeCases(unittest.TestCase):
    """Edge cases for duplicate receipt detection."""

    def setUp(self):
        from app.services.dedup_service import DedupService
        self.svc = DedupService()

    # ── content fingerprint ──────────────────────────────────────────────

    def test_fingerprint_all_empty_codes_returns_empty(self):
        """BUG FIX REGRESSION: items with empty/None codes must return ''."""
        items = [
            {"code": "", "quantity": 3},
            {"code": "", "quantity": 5},
            {"quantity": 2},          # missing 'code' key
        ]
        fp = self.svc.compute_content_fingerprint(items)
        self.assertEqual(fp, "")

    def test_fingerprint_mixed_empty_and_valid(self):
        """Items with some empty codes should only fingerprint the valid ones."""
        items_full = [{"code": "ABC", "quantity": 2}]
        items_mixed = [
            {"code": "", "quantity": 99},
            {"code": "ABC", "quantity": 2},
            {"code": None, "quantity": 1},    # None code → treated as ""
        ]
        # The fingerprint should be the same since only "ABC:2.0" contributes
        fp_full = self.svc.compute_content_fingerprint(items_full)
        fp_mixed = self.svc.compute_content_fingerprint(items_mixed)
        self.assertEqual(fp_full, fp_mixed)

    def test_fingerprint_whitespace_codes(self):
        """Whitespace-only codes should be filtered out."""
        items = [{"code": "   ", "quantity": 5}]
        fp = self.svc.compute_content_fingerprint(items)
        self.assertEqual(fp, "")

    def test_fingerprint_case_insensitive(self):
        """Codes differing only in case should produce same fingerprint."""
        fp_lower = self.svc.compute_content_fingerprint([{"code": "abc", "quantity": 1}])
        fp_upper = self.svc.compute_content_fingerprint([{"code": "ABC", "quantity": 1}])
        self.assertEqual(fp_lower, fp_upper)

    def test_fingerprint_float_rounding(self):
        """Quantities that round to same value should produce same fingerprint."""
        fp_a = self.svc.compute_content_fingerprint([{"code": "X", "quantity": 2.04}])
        fp_b = self.svc.compute_content_fingerprint([{"code": "X", "quantity": 2.05}])
        # round(2.04, 1) = 2.0, round(2.05, 1) = 2.0  (banker's rounding in Python)
        self.assertEqual(fp_a, fp_b)

    def test_fingerprint_special_chars_in_code(self):
        """Codes with special characters should still work."""
        fp = self.svc.compute_content_fingerprint([{"code": "A/B-C.D", "quantity": 1}])
        self.assertEqual(len(fp), 32)

    def test_fingerprint_very_large_item_list(self):
        """Fingerprint should handle large item lists."""
        items = [{"code": f"ITEM{i:04d}", "quantity": i} for i in range(500)]
        fp = self.svc.compute_content_fingerprint(items)
        self.assertEqual(len(fp), 32)

    # ── hamming distance ─────────────────────────────────────────────────

    def test_hamming_distance_non_hex_chars(self):
        """Non-hex characters should return max distance."""
        d = self.svc.hamming_distance("gggg", "0000")
        self.assertEqual(d, 64)

    def test_hamming_distance_mixed_length(self):
        """Hashes of different lengths should be zero-padded."""
        d = self.svc.hamming_distance("ff", "00ff")
        self.assertEqual(d, 0)  # both represent the same integer after zfill

    def test_hamming_distance_both_empty(self):
        """Both empty should return max distance."""
        self.assertEqual(self.svc.hamming_distance("", ""), 64)

    # ── check_duplicate ──────────────────────────────────────────────────

    def test_check_duplicate_null_hashes_from_db(self):
        """BUG FIX REGRESSION: DB rows with NULL hashes should not crash."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 99,
                "receipt_number": "REC-001",
                "image_hash": None,           # SQL NULL
                "content_fingerprint": None,  # SQL NULL
                "created_at": "2026-03-17 12:00:00",
            }
        ]
        # Should not crash, should return None (no match)
        result = self.svc.check_duplicate("abc123", "fp123", mock_db)
        self.assertIsNone(result)

    def test_check_duplicate_empty_new_hashes(self):
        """Empty new hashes should not crash or false-match."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 1, "receipt_number": "R1",
                "image_hash": "abc123", "content_fingerprint": "fp",
                "created_at": "2026-03-17",
            }
        ]
        result = self.svc.check_duplicate("", "", mock_db)
        self.assertIsNone(result)

    def test_check_duplicate_picks_best_match(self):
        """When multiple receipts match, the highest score should win."""
        mock_db = MagicMock()
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 1, "receipt_number": "R1",
                "image_hash": "0000000000000003",  # hamming=2 from 0000000000000000
                "content_fingerprint": "",
                "created_at": "2026-03-17",
            },
            {
                "id": 2, "receipt_number": "R2",
                "image_hash": "0000000000000000",  # hamming=0 → score 60
                "content_fingerprint": "my_fp",    # + 40 → score 100
                "created_at": "2026-03-17",
            },
        ]
        result = self.svc.check_duplicate("0000000000000000", "my_fp", mock_db)
        self.assertIsNotNone(result)
        self.assertEqual(result["similar_receipt_id"], 2)
        self.assertEqual(result["confidence"], 100)

    def test_check_duplicate_near_threshold_hash(self):
        """Hamming distance exactly at threshold (5) → should still match."""
        mock_db = MagicMock()
        # 0x1f = 0b11111 → 5 bits set
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 10, "receipt_number": "R10",
                "image_hash": "000000000000001f",
                "content_fingerprint": "",
                "created_at": "2026-03-17",
            },
        ]
        result = self.svc.check_duplicate("0000000000000000", "", mock_db)
        self.assertIsNotNone(result)  # distance=5 ≤ threshold=5 → match

    def test_check_duplicate_just_above_threshold(self):
        """Hamming distance of 6 → should NOT match on image alone."""
        mock_db = MagicMock()
        # 0x3f = 0b111111 → 6 bits set
        mock_db.get_recent_receipts_with_hashes.return_value = [
            {
                "id": 11, "receipt_number": "R11",
                "image_hash": "000000000000003f",
                "content_fingerprint": "",
                "created_at": "2026-03-17",
            },
        ]
        result = self.svc.check_duplicate("0000000000000000", "", mock_db)
        self.assertIsNone(result)  # distance=6 > threshold=5

    # ── image hash ───────────────────────────────────────────────────────

    def test_image_hash_different_images(self):
        """Different images should produce different hashes."""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")

        tmp1 = tempfile.mktemp(suffix=".png")
        tmp2 = tempfile.mktemp(suffix=".png")
        try:
            # Create images with clearly different content (not solid colors)
            img1 = Image.new("L", (64, 64), color=0)
            # Draw a white rectangle on the left half
            for y in range(64):
                for x in range(32):
                    img1.putpixel((x, y), 255)
            img1.save(tmp1)

            img2 = Image.new("L", (64, 64), color=0)
            # Draw a white rectangle on the bottom half
            for y in range(32, 64):
                for x in range(64):
                    img2.putpixel((x, y), 255)
            img2.save(tmp2)

            h1 = self.svc.compute_image_hash(tmp1)
            h2 = self.svc.compute_image_hash(tmp2)
            self.assertNotEqual(h1, h2)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp1)
            with contextlib.suppress(OSError):
                os.unlink(tmp2)

    def test_image_hash_corrupted_file(self):
        """Corrupted/non-image file should return empty string."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, mode="w") as f:
            f.write("this is not an image")
            tmp = f.name
        try:
            h = self.svc.compute_image_hash(tmp)
            self.assertEqual(h, "")
        finally:
            os.unlink(tmp)

    def test_image_hash_empty_file(self):
        """Empty file should return empty string."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = f.name
        try:
            h = self.svc.compute_image_hash(tmp)
            self.assertEqual(h, "")
        finally:
            os.unlink(tmp)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Quality Scorer — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestQualityScorerEdgeCases(unittest.TestCase):
    """Edge cases for receipt quality scoring."""

    def setUp(self):
        from app.ocr.quality_scorer import QualityScorer
        self.scorer = QualityScorer()

    def test_items_without_match_type_not_counted(self):
        """BUG FIX REGRESSION: items without match_type should NOT count as matched."""
        items = [
            {"code": "A", "quantity": 1},              # no match_type key
            {"code": "B", "quantity": 1},              # no match_type key
            {"code": "C", "quantity": 1, "match_type": "exact"},
        ]
        result = self.scorer.score(items, {"ocr_avg_confidence": 0.9})
        # Only 1 of 3 items has a valid match_type → match_rate ~33%
        self.assertAlmostEqual(
            result["breakdown"]["catalog_match"]["match_rate"],
            1 / 3,
            places=2,
        )

    def test_sharpness_zero_displays_correctly(self):
        """BUG FIX REGRESSION: sharpness=0 should display as 0.0, not None."""
        items = [{"code": "A", "quantity": 1, "match_type": "exact"}]
        metadata = {
            "ocr_avg_confidence": 0.9,
            "preprocessing": {"quality": {"sharpness": 0, "brightness": 0}},
        }
        result = self.scorer.score(items, metadata)
        bd = result["breakdown"]["image_quality"]
        self.assertEqual(bd["sharpness"], 0.0)
        self.assertEqual(bd["brightness"], 0.0)

    def test_confidence_at_boundary_050(self):
        """Confidence exactly 0.5 → 0 points."""
        result = self.scorer.score([], {"ocr_avg_confidence": 0.5})
        self.assertEqual(result["breakdown"]["ocr_confidence"]["score"], 0.0)

    def test_confidence_at_boundary_100(self):
        """Confidence exactly 1.0 → 30 points."""
        items = [{"code": "A", "quantity": 1, "match_type": "exact"}]
        result = self.scorer.score(items, {"ocr_avg_confidence": 1.0})
        self.assertEqual(result["breakdown"]["ocr_confidence"]["score"], 30.0)

    def test_confidence_above_100(self):
        """Confidence > 1.0 should be capped at 30 points."""
        result = self.scorer.score([], {"ocr_avg_confidence": 1.5})
        self.assertEqual(result["breakdown"]["ocr_confidence"]["score"], 30.0)

    def test_negative_confidence(self):
        """Negative confidence should give 0 points."""
        result = self.scorer.score([], {"ocr_avg_confidence": -0.5})
        self.assertEqual(result["breakdown"]["ocr_confidence"]["score"], 0.0)

    def test_none_metadata_values(self):
        """None values in metadata should not crash."""
        result = self.scorer.score([], {"ocr_avg_confidence": None})
        # None treated as falsy → conf_score = 0
        self.assertIsNotNone(result["score"])

    def test_empty_metadata(self):
        """Completely empty metadata dict should still work."""
        result = self.scorer.score([], {})
        self.assertGreaterEqual(result["score"], 0)
        self.assertIn(result["grade"], ("A", "B", "C", "D"))

    def test_total_verification_unknown_status(self):
        """Unknown verification status should give 0 points."""
        result = self.scorer.score([], {"ocr_avg_confidence": 0.9},
                                   total_verification={"verification_status": "banana"})
        self.assertEqual(result["breakdown"]["total_verification"]["score"], 0.0)

    def test_math_verification_no_prices(self):
        """Math verification without prices → neutral score."""
        result = self.scorer.score([], {"ocr_avg_confidence": 0.9},
                                   math_verification={"has_prices": False})
        self.assertEqual(result["breakdown"]["math_verification"]["score"], 7.0)

    def test_grade_boundary_exactly_90(self):
        """Score exactly 90 → Grade A."""
        # Engineer inputs to get exactly 90: conf=30 + items=20 + total=15 + math=15 + img=10 + cat=0 = 90
        items = [
            {"code": "A", "quantity": 1, "match_type": "unknown"},
            {"code": "B", "quantity": 1, "match_type": "unknown"},
            {"code": "C", "quantity": 1, "match_type": "unknown"},
        ]
        metadata = {
            "ocr_avg_confidence": 1.0,
            "preprocessing": {"quality": {"sharpness": 200, "brightness": 130}},
        }
        tv = {"verification_status": "verified"}
        mv = {"has_prices": True, "all_line_math_ok": True}
        result = self.scorer.score(items, metadata, tv, mv)
        self.assertEqual(result["score"], 90.0)
        self.assertEqual(result["grade"], "A")

    def test_grade_boundary_exactly_75(self):
        """Score exactly 75 → Grade B."""
        # conf=18(0.8) + items=20(3) + total=15(verified) + math=7(neutral) + img=5(neutral) + cat=10 = 75
        items = [
            {"code": "A", "quantity": 1, "match_type": "exact"},
            {"code": "B", "quantity": 1, "match_type": "exact"},
            {"code": "C", "quantity": 1, "match_type": "exact"},
        ]
        result = self.scorer.score(items, {"ocr_avg_confidence": 0.8},
                                   total_verification={"verification_status": "verified"})
        self.assertEqual(result["score"], 75.0)
        self.assertEqual(result["grade"], "B")

    def test_score_never_exceeds_100(self):
        """Total score should be capped at 100."""
        items = [{"code": "A", "quantity": 1, "match_type": "exact", "confidence": 1.0}
                 for _ in range(10)]
        metadata = {
            "ocr_avg_confidence": 1.0,
            "preprocessing": {"quality": {"sharpness": 999, "brightness": 130}},
        }
        result = self.scorer.score(items, metadata,
                                   {"verification_status": "verified"},
                                   {"has_prices": True, "all_line_math_ok": True})
        self.assertLessEqual(result["score"], 100)

    def test_extreme_low_brightness(self):
        """Very low brightness → only 1 point for image quality."""
        items = [{"code": "A", "quantity": 1, "match_type": "exact"}]
        metadata = {
            "ocr_avg_confidence": 0.9,
            "preprocessing": {"quality": {"sharpness": 200, "brightness": 10}},
        }
        result = self.scorer.score(items, metadata)
        # sharpness 200 → 5pts, brightness 10 (< 40) → 1pt = 6 total
        self.assertEqual(result["breakdown"]["image_quality"]["score"], 6)

    def test_medium_brightness_band(self):
        """Brightness in 40-60 or 200-220 range → 3 points."""
        items = [{"code": "A", "quantity": 1, "match_type": "exact"}]
        metadata = {
            "ocr_avg_confidence": 0.9,
            "preprocessing": {"quality": {"sharpness": 0, "brightness": 45}},
        }
        result = self.scorer.score(items, metadata)
        # sharpness 0 → 0pts, brightness 45 (40-60 band) → 3pts = 3
        self.assertEqual(result["breakdown"]["image_quality"]["score"], 3)

    def test_all_unknown_match_type(self):
        """All items are 'unknown' → 0 catalog match points."""
        items = [
            {"code": "A", "quantity": 1, "match_type": "unknown"},
            {"code": "B", "quantity": 1, "match_type": "azure-unmatched"},
        ]
        result = self.scorer.score(items, {"ocr_avg_confidence": 0.9})
        self.assertEqual(result["breakdown"]["catalog_match"]["score"], 0.0)
        self.assertEqual(result["breakdown"]["catalog_match"]["match_rate"], 0.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Receipt Validator — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidatorEdgeCases(unittest.TestCase):
    """Edge cases for the validation rules engine."""

    def setUp(self):
        from app.ocr.validators import ReceiptValidator
        self.validator = ReceiptValidator()

    def test_empty_items_list(self):
        """Empty items list should return valid with no warnings."""
        result = self.validator.validate([])
        self.assertTrue(result["valid"])
        self.assertEqual(result["summary"]["total_warnings"], 0)

    def test_anomaly_detection_without_catalog(self):
        """BUG FIX REGRESSION: Rule 4 should work without catalog."""
        items = [{"code": "ABC", "quantity": 100, "needs_review": False}]
        historical = {"ABC": {"avg_quantity": 3, "max_quantity": 10, "count": 20}}
        # catalog is None, but anomaly detection should still work
        result = self.validator.validate(items, catalog=None, historical_stats=historical)
        warnings = [w for w in result["warnings"] if w["rule"] == "quantity_anomaly"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(items[0]["needs_review"])

    def test_high_qty_with_price_not_flagged(self):
        """High quantity WITH a unit_price should NOT trigger suspicious_quantity."""
        items = [{"code": "ABC", "quantity": 500, "unit_price": 10, "needs_review": False}]
        result = self.validator.validate(items)
        suspicious = [w for w in result["warnings"] if w["rule"] == "suspicious_quantity"]
        self.assertEqual(len(suspicious), 0)

    def test_price_at_exact_deviation_threshold(self):
        """Price at exactly 5× catalog should be flagged."""
        items = [
            {"code": "ABC", "quantity": 1, "unit_price": 1000, "line_total": 1000, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        # ratio = 1000/200 = 5.0, threshold is 5.0
        # > 5.0 check is strict → 5.0 is NOT > 5.0, so NO warning
        result = self.validator.validate(items, catalog=catalog)
        deviation = [w for w in result["warnings"] if w["rule"] == "price_deviation"]
        self.assertEqual(len(deviation), 0)

    def test_price_just_above_deviation_threshold(self):
        """Price at 5.01× catalog should be flagged."""
        items = [
            {"code": "ABC", "quantity": 1, "unit_price": 1001, "line_total": 1001, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog)
        deviation = [w for w in result["warnings"] if w["rule"] == "price_deviation"]
        self.assertEqual(len(deviation), 1)

    def test_price_below_deviation_threshold(self):
        """Very low price (< 1/5 of catalog) should be flagged."""
        items = [
            {"code": "ABC", "quantity": 1, "unit_price": 30, "line_total": 30, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        # ratio = 30/200 = 0.15 < 0.2 → flagged
        result = self.validator.validate(items, catalog=catalog)
        deviation = [w for w in result["warnings"] if w["rule"] == "price_deviation"]
        self.assertEqual(len(deviation), 1)

    def test_line_total_within_tolerance(self):
        """Line total within 1% tolerance should NOT warn."""
        items = [
            {"code": "ABC", "quantity": 10, "unit_price": 199, "line_total": 1990, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 199}}
        # expected = 10*199 = 1990, actual = 1990 → exact match
        result = self.validator.validate(items, catalog=catalog)
        mismatches = [w for w in result["warnings"] if w["rule"] == "line_total_mismatch"]
        self.assertEqual(len(mismatches), 0)

    def test_line_total_small_expected_tolerance(self):
        """For small expected totals, tolerance floor of 1.0 should apply."""
        items = [
            {"code": "ABC", "quantity": 1, "unit_price": 5, "line_total": 5.5, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 5}}
        # expected = 5, tolerance = max(1.0, 5*0.01) = max(1.0, 0.05) = 1.0
        # |5.5 - 5| = 0.5 ≤ 1.0 → no warning
        result = self.validator.validate(items, catalog=catalog)
        mismatches = [w for w in result["warnings"] if w["rule"] == "line_total_mismatch"]
        self.assertEqual(len(mismatches), 0)

    def test_multiple_rules_same_item(self):
        """Multiple rules can trigger on the same item."""
        items = [
            {"code": "ABC", "quantity": 0, "unit_price": 99999, "line_total": 1, "needs_review": False},
            {"code": "ABC", "quantity": 1, "unit_price": 0, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog)
        # Item 0: zero_qty (high) + price_deviation (medium) + line_total_mismatch (low) + duplicate (low)
        # Item 1: auto-fill (correction) + duplicate (low)
        self.assertGreater(result["summary"]["total_warnings"], 2)
        self.assertGreater(result["summary"]["auto_corrections"], 0)

    def test_catalog_item_not_in_catalog(self):
        """Items not in catalog should skip price checks."""
        items = [
            {"code": "UNKNOWN", "quantity": 2, "unit_price": 99999, "line_total": 199998, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 200}}
        result = self.validator.validate(items, catalog=catalog)
        deviation = [w for w in result["warnings"] if w["rule"] == "price_deviation"]
        self.assertEqual(len(deviation), 0)

    def test_anomaly_detection_below_3x(self):
        """Qty at 2.9× max → should NOT trigger anomaly."""
        items = [{"code": "ABC", "quantity": 29, "needs_review": False}]
        historical = {"ABC": {"avg_quantity": 5, "max_quantity": 10, "count": 20}}
        # 29 ≤ 10*3 = 30 → no anomaly
        result = self.validator.validate(items, historical_stats=historical)
        anomalies = [w for w in result["warnings"] if w["rule"] == "quantity_anomaly"]
        self.assertEqual(len(anomalies), 0)

    def test_anomaly_detection_at_3x(self):
        """Qty exactly at 3× max → should NOT trigger (must exceed 3×)."""
        items = [{"code": "ABC", "quantity": 30, "needs_review": False}]
        historical = {"ABC": {"avg_quantity": 5, "max_quantity": 10, "count": 20}}
        result = self.validator.validate(items, historical_stats=historical)
        anomalies = [w for w in result["warnings"] if w["rule"] == "quantity_anomaly"]
        self.assertEqual(len(anomalies), 0)

    def test_anomaly_detection_above_3x(self):
        """Qty at 3.1× max → should trigger anomaly."""
        items = [{"code": "ABC", "quantity": 31, "needs_review": False}]
        historical = {"ABC": {"avg_quantity": 5, "max_quantity": 10, "count": 20}}
        result = self.validator.validate(items, historical_stats=historical)
        anomalies = [w for w in result["warnings"] if w["rule"] == "quantity_anomaly"]
        self.assertEqual(len(anomalies), 1)

    def test_anomaly_zero_historical_max(self):
        """Zero historical max should not trigger division issues."""
        items = [{"code": "ABC", "quantity": 5, "needs_review": False}]
        historical = {"ABC": {"avg_quantity": 0, "max_quantity": 0, "count": 0}}
        result = self.validator.validate(items, historical_stats=historical)
        anomalies = [w for w in result["warnings"] if w["rule"] == "quantity_anomaly"]
        self.assertEqual(len(anomalies), 0)

    def test_duplicate_three_copies(self):
        """Item appearing 3 times should show 'appears 3 times'."""
        items = [
            {"code": "ABC", "quantity": 1, "needs_review": False},
            {"code": "ABC", "quantity": 2, "needs_review": False},
            {"code": "ABC", "quantity": 3, "needs_review": False},
        ]
        result = self.validator.validate(items)
        dups = [w for w in result["warnings"] if w["rule"] == "duplicate_item"]
        self.assertEqual(len(dups), 1)
        self.assertIn("appears 3 times", dups[0]["message"])

    def test_items_with_no_code_key(self):
        """Items without 'code' key should not crash."""
        items = [
            {"quantity": 5, "needs_review": False},
        ]
        result = self.validator.validate(items)
        # qty 5 is fine, no crash expected
        self.assertTrue(result["valid"])

    def test_catalog_price_zero(self):
        """Catalog entry with unit_price=0 should not trigger auto-fill."""
        items = [
            {"code": "ABC", "quantity": 2, "unit_price": 0, "line_total": 0, "needs_review": False},
        ]
        catalog = {"ABC": {"unit_price": 0}}
        result = self.validator.validate(items, catalog=catalog)
        corrections = [c for c in result["corrections"] if c["reason"] == "catalog_price_auto_fill"]
        self.assertEqual(len(corrections), 0)

    def test_valid_flag_depends_on_high_severity_only(self):
        """valid=False only when there are HIGH severity warnings."""
        items = [
            {"code": "ABC", "quantity": 200, "needs_review": False},     # medium (suspicious)
            {"code": "ABC", "quantity": 1, "needs_review": False},       # low (duplicate)
        ]
        result = self.validator.validate(items)
        # Only medium + low → still valid
        self.assertTrue(result["valid"])
        self.assertEqual(result["summary"]["high"], 0)
        self.assertGreater(result["summary"]["medium"], 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Correction Service — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCorrectionEdgeCases(unittest.TestCase):
    """Edge cases for the OCR correction feedback loop."""

    def setUp(self):
        from app.services.correction_service import CorrectionService
        self.svc = CorrectionService()

    def test_record_correction_empty_codes(self):
        """Empty string codes should detect no change (both empty)."""
        mock_db = MagicMock()
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=1,
            original_code="", corrected_code="",
            original_qty=1, corrected_qty=1,
        )
        mock_db.add_ocr_correction.assert_not_called()

    def test_record_correction_case_only_change(self):
        """Case-only change (abc→ABC) should NOT be recorded."""
        mock_db = MagicMock()
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=1,
            original_code="abc", corrected_code="ABC",
            original_qty=1, corrected_qty=1,
        )
        mock_db.add_ocr_correction.assert_not_called()

    def test_record_correction_tiny_qty_change_ignored(self):
        """Qty change < 0.01 should be ignored (floating point noise)."""
        mock_db = MagicMock()
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=1,
            original_code="ABC", corrected_code="ABC",
            original_qty=3.0, corrected_qty=3.005,  # diff=0.005 < 0.01
        )
        mock_db.add_ocr_correction.assert_not_called()

    def test_record_correction_db_error_graceful(self):
        """DB error during recording should not raise."""
        mock_db = MagicMock()
        mock_db.add_ocr_correction.side_effect = Exception("DB locked")
        # Should not raise — error is caught internally
        self.svc.record_correction(
            mock_db, receipt_id=1, item_id=1,
            original_code="ABC", corrected_code="XYZ",
            original_qty=1, corrected_qty=1,
        )
        # But cache should still be invalidated (record was attempted)
        # Actually, the exception happens DURING add_ocr_correction, so the
        # cache invalidation (which is in the except block) might not happen.
        # Let's verify: the except catches and warns but does NOT invalidate.
        # This is actually correct: if the DB write failed, no cache to invalidate.

    def test_apply_correction_whitespace_code(self):
        """Whitespace-padded code should be stripped before lookup."""
        corrections = {"ABC": "XYZ"}
        code, was = self.svc.apply_correction("  abc  ", corrections)
        self.assertEqual(code, "XYZ")
        self.assertTrue(was)

    def test_apply_correction_empty_map(self):
        """Empty corrections map → no correction."""
        code, was = self.svc.apply_correction("ABC", {})
        self.assertEqual(code, "ABC")
        self.assertFalse(was)

    def test_apply_correction_empty_code(self):
        """Empty code should pass through."""
        code, was = self.svc.apply_correction("", {"": "XYZ"})
        # upper("") = "", which IS in map
        self.assertEqual(code, "XYZ")
        self.assertTrue(was)

    def test_get_corrections_map_db_error(self):
        """DB error should return empty map, not crash."""
        mock_db = MagicMock()
        mock_db.get_ocr_corrections_map.side_effect = Exception("connection lost")
        result = self.svc.get_corrections_map(mock_db)
        self.assertEqual(result, {})

    def test_concurrent_cache_access(self):
        """Concurrent cache access should not cause data corruption."""
        mock_db = MagicMock()
        mock_db.get_ocr_corrections_map.return_value = {"A": "B"}
        results = []
        errors = []

        def worker():
            try:
                r = self.svc.get_corrections_map(mock_db)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 20)
        for r in results:
            self.assertEqual(r, {"A": "B"})

    def test_get_correction_stats_structure(self):
        """Stats fallback should have correct structure."""
        mock_db = MagicMock()
        mock_db.get_ocr_correction_stats.side_effect = Exception("boom")
        stats = self.svc.get_correction_stats(mock_db)
        self.assertIn("total_corrections", stats)
        self.assertIn("unique_patterns", stats)
        self.assertIn("top_corrections", stats)
        self.assertIsInstance(stats["top_corrections"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Parser Date Extraction — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDateExtractionEdgeCases(unittest.TestCase):
    """Edge cases for receipt date extraction."""

    def setUp(self):
        from app.ocr.parser import ReceiptParser
        self.parser = ReceiptParser({"ABC": "Test"})

    def _lines(self, texts):
        return [{"text": t, "confidence": 0.9, "y_center": i * 50} for i, t in enumerate(texts)]

    def test_date_embedded_in_text(self):
        """Date embedded in a longer text line should still be found."""
        result = self.parser._extract_receipt_date(self._lines([
            "Invoice #123 Date: 15/03/2026 Processed",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_multiple_dates_keyword_wins(self):
        """When multiple dates exist, the keyword line should win."""
        result = self.parser._extract_receipt_date(self._lines([
            "10/01/2025",           # non-keyword date
            "Date: 15/03/2026",     # keyword date
            "20/12/2024",           # non-keyword date
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_date_february_29_leap_year(self):
        """Feb 29 should be accepted (no full leap year validation)."""
        result = self.parser._extract_receipt_date(self._lines([
            "Date: 29/02/2024",  # 2024 is leap year
        ]))
        self.assertEqual(result, "2024-02-29")

    def test_date_february_30_accepted_but_invalid(self):
        """Feb 30 passes range check (1-31) but is calendrically invalid.
        The parser doesn't do full calendar validation — acceptable limitation."""
        result = self.parser._extract_receipt_date(self._lines([
            "30/02/2026",
        ]))
        # 30 ≤ 31 and 02 ≤ 12 → passes range check → "2026-02-30" returned
        # This is a known limitation — we don't do full calendar validation
        self.assertEqual(result, "2026-02-30")

    def test_date_year_out_of_range(self):
        """Year outside 1900-2100 should be rejected."""
        result = self.parser._extract_receipt_date(self._lines([
            "15/03/1899",  # before 1900
        ]))
        self.assertIsNone(result)

    def test_date_year_1900_boundary(self):
        """Year exactly 1900 should be accepted."""
        result = self.parser._extract_receipt_date(self._lines([
            "01/01/1900",
        ]))
        self.assertEqual(result, "1900-01-01")

    def test_date_year_2100_boundary(self):
        """Year exactly 2100 should be accepted."""
        result = self.parser._extract_receipt_date(self._lines([
            "31/12/2100",
        ]))
        self.assertEqual(result, "2100-12-31")

    def test_date_two_digit_year_not_matched(self):
        """Two-digit year (e.g. 15/03/26) should NOT be matched by pattern 0."""
        result = self.parser._extract_receipt_date(self._lines([
            "15/03/26",  # \d{4} requires 4 digits
        ]))
        self.assertIsNone(result)

    def test_date_month_name_full(self):
        """Full month name (e.g. 'January') should work."""
        result = self.parser._extract_receipt_date(self._lines([
            "January 5, 2026",
        ]))
        self.assertEqual(result, "2026-01-05")

    def test_date_month_abbreviation(self):
        """Month abbreviation with extra letters should work."""
        result = self.parser._extract_receipt_date(self._lines([
            "5 September 2025",
        ]))
        self.assertEqual(result, "2025-09-05")

    def test_price_not_detected_as_date(self):
        """A price like '1234.56' should NOT be detected as a date."""
        result = self.parser._extract_receipt_date(self._lines([
            "Total: 1234.56",
        ]))
        self.assertIsNone(result)

    def test_empty_lines_list(self):
        """Empty lines list should return None."""
        result = self.parser._extract_receipt_date([])
        self.assertIsNone(result)

    def test_blank_text_lines(self):
        """Lines with only whitespace should be skipped."""
        result = self.parser._extract_receipt_date(self._lines([
            "   ",
            "",
        ]))
        self.assertIsNone(result)

    def test_date_with_time(self):
        """Date followed by time should still be extracted."""
        result = self.parser._extract_receipt_date(self._lines([
            "Date: 15/03/2026 14:30:00",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_dt_keyword(self):
        """'Dt:' as keyword variant should give priority."""
        result = self.parser._extract_receipt_date(self._lines([
            "01/01/2025",
            "Dt: 15/03/2026",
        ]))
        self.assertEqual(result, "2026-03-15")

    def test_invoice_date_keyword(self):
        """'Invoice Date' as keyword should give priority."""
        result = self.parser._extract_receipt_date(self._lines([
            "01/01/2025",
            "Invoice Date: 15/03/2026",
        ]))
        self.assertEqual(result, "2026-03-15")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Parser Store Extraction — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStoreExtractionEdgeCases(unittest.TestCase):
    """Edge cases for store/merchant name extraction."""

    def setUp(self):
        from app.ocr.parser import ReceiptParser
        self.parser = ReceiptParser({"ABC": "Test", "XYZ": "Other"})

    def _lines(self, texts):
        return [{"text": t, "confidence": 0.9, "y_center": i * 50} for i, t in enumerate(texts)]

    def test_long_store_name_truncated(self):
        """Store name > 200 chars should be truncated."""
        long_name = "A" * 300
        result = self.parser._extract_store_name(self._lines([long_name]))
        self.assertEqual(len(result), 200)

    def test_separator_lines_skipped(self):
        """Lines of dashes/equals/stars should be skipped."""
        for sep in ["----------", "===========", "***********", "#####"]:
            result = self.parser._extract_store_name(self._lines([
                sep,
                "REAL STORE NAME",
            ]))
            self.assertEqual(result, "REAL STORE NAME", f"Failed for separator: {sep}")

    def test_receipt_number_line_skipped(self):
        """Lines starting with 'Receipt #' should be skipped."""
        result = self.parser._extract_store_name(self._lines([
            "Receipt #12345",
            "MY PAINT STORE",
        ]))
        self.assertEqual(result, "MY PAINT STORE")

    def test_invoice_line_skipped(self):
        """Lines starting with 'Invoice #' should be skipped."""
        result = self.parser._extract_store_name(self._lines([
            "Invoice #67890",
            "MY PAINT STORE",
        ]))
        self.assertEqual(result, "MY PAINT STORE")

    def test_numbered_item_line_skipped(self):
        """Lines starting with a number like '1.' or '2)' should be skipped."""
        result = self.parser._extract_store_name(self._lines([
            "1. First item",
            "REAL STORE",
        ]))
        self.assertEqual(result, "REAL STORE")

    def test_date_only_line_skipped(self):
        """Pure date lines (e.g. '15/03/2026') should be skipped."""
        result = self.parser._extract_store_name(self._lines([
            "15/03/2026",
            "SHARMA STORE",
        ]))
        self.assertEqual(result, "SHARMA STORE")

    def test_only_noise_lines_returns_none(self):
        """If all top 5 lines are noise, return None."""
        result = self.parser._extract_store_name(self._lines([
            "AB",                  # too short
            "----",                # separator
            "Date: 15/03/2026",    # date keyword
            "15/03/2026",          # date line
            "ABC",                 # product code
        ]))
        self.assertIsNone(result)

    def test_store_after_5th_line_not_detected(self):
        """Store name on line 6+ should NOT be detected."""
        lines = self._lines([
            "AB", "CD", "EF", "GH", "IJ",  # 5 lines < 3 chars
            "REAL STORE ON LINE 6",
        ])
        result = self.parser._extract_store_name(lines)
        self.assertIsNone(result)

    def test_mixed_header_and_non_header_tokens(self):
        """Line with a mix of header and non-header tokens should NOT be skipped."""
        result = self.parser._extract_store_name(self._lines([
            "Grand Total Amount",  # "grand" is not in header_words
        ]))
        self.assertIsNotNone(result)

    def test_store_with_special_characters(self):
        """Store names with special characters should be preserved."""
        result = self.parser._extract_store_name(self._lines([
            "M/s. SHARMA & SONS (PVT) LTD.",
        ]))
        self.assertEqual(result, "M/s. SHARMA & SONS (PVT) LTD.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Database — Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDatabaseEdgeCases(unittest.TestCase):
    """Edge cases for Smart OCR database methods."""

    def setUp(self):
        from app.database import Database
        self.db = Database()

    def test_update_metadata_no_valid_keys(self):
        """Passing only disallowed keys should be a no-op (no SQL injection)."""
        rid = self.db.create_receipt("TEST-EDGE-NOOP")
        # This should do nothing, not crash, not inject
        self.db.update_receipt_metadata(
            rid,
            DROP_TABLE="receipts",
            __sql_injection__="1; DROP TABLE receipts;--",
        )
        # Receipt should still exist
        r = self.db.get_receipt(rid)
        self.assertIsNotNone(r)
        self.db.delete_receipt(rid)

    def test_update_metadata_empty_kwargs(self):
        """Empty kwargs should be a no-op."""
        rid = self.db.create_receipt("TEST-EDGE-EMPTY")
        self.db.update_receipt_metadata(rid)  # no kwargs
        r = self.db.get_receipt(rid)
        self.assertIsNotNone(r)
        self.db.delete_receipt(rid)

    def test_update_metadata_partial(self):
        """Updating only some metadata fields should work."""
        rid = self.db.create_receipt("TEST-EDGE-PARTIAL")
        self.db.update_receipt_metadata(rid, quality_score=42)
        r = self.db.get_receipt(rid)
        self.assertEqual(r.get("quality_score"), 42)
        self.assertIsNone(r.get("image_hash") or None)  # not set → default ''
        self.db.delete_receipt(rid)

    def test_update_metadata_overwrite(self):
        """Updating same field twice should keep last value."""
        rid = self.db.create_receipt("TEST-EDGE-OVERWRITE")
        self.db.update_receipt_metadata(rid, quality_score=10)
        self.db.update_receipt_metadata(rid, quality_score=99)
        r = self.db.get_receipt(rid)
        self.assertEqual(r.get("quality_score"), 99)
        self.db.delete_receipt(rid)

    def test_get_receipt_item_negative_id(self):
        """Negative item ID should return None."""
        result = self.db.get_receipt_item(-1)
        self.assertIsNone(result)

    def test_corrections_map_min_count_zero(self):
        """min_count=0 should return ALL corrections (even single ones)."""
        suffix = uuid.uuid4().hex[:6].upper()
        rid = self.db.create_receipt(f"TEST-MC0-{suffix}")
        self.db.add_ocr_correction(rid, 0, f"ORIG_{suffix}", f"CORR_{suffix}", 1, 1)
        result = self.db.get_ocr_corrections_map(min_count=0)
        self.assertIn(f"ORIG_{suffix}", result)
        self.db.delete_receipt(rid)

    def test_item_quantity_stats_single_occurrence_excluded(self):
        """Products with only 1 occurrence should be excluded (HAVING cnt >= 2)."""
        suffix = uuid.uuid4().hex[:6].upper()
        code = f"SINGLE_{suffix}"
        rid = self.db.create_receipt(f"TEST-SINGLE-{suffix}")
        self.db.add_receipt_items(rid, [
            {"code": code, "product": "T", "quantity": 5,
             "unit": "Piece", "confidence": 0.9, "unit_price": 0, "line_total": 0},
        ])
        stats = self.db.get_item_quantity_stats()
        self.assertNotIn(code, stats)  # only 1 occurrence → excluded
        self.db.delete_receipt(rid)

    def test_recent_receipts_hashes_includes_today(self):
        """Receipts created right now should appear in 24-hour window."""
        suffix = uuid.uuid4().hex[:6].upper()
        rid = self.db.create_receipt(f"TEST-RECENT-{suffix}")
        results = self.db.get_recent_receipts_with_hashes(hours=24)
        found = [r for r in results if r["receipt_number"] == f"TEST-RECENT-{suffix}"]
        self.assertEqual(len(found), 1)
        self.db.delete_receipt(rid)

    def test_recent_receipts_hashes_zero_hours(self):
        """Zero-hour window should still return results (edge)."""
        results = self.db.get_recent_receipts_with_hashes(hours=0)
        self.assertIsInstance(results, list)

    def test_correction_stats_empty_table(self):
        """Stats should work even when corrections table is empty (or has data)."""
        stats = self.db.get_ocr_correction_stats()
        self.assertIn("total_corrections", stats)
        self.assertIn("unique_patterns", stats)
        self.assertIn("top_corrections", stats)
        self.assertIsInstance(stats["total_corrections"], int)

    def test_add_correction_same_code_different_targets(self):
        """Same original code corrected to different targets → highest count wins."""
        suffix = uuid.uuid4().hex[:6].upper()
        rid = self.db.create_receipt(f"TEST-MULTI-{suffix}")
        orig = f"MULTI_{suffix}"
        # Correct to "A" 3 times
        for _ in range(3):
            self.db.add_ocr_correction(rid, 0, orig, f"A_{suffix}", 1, 1)
        # Correct to "B" 1 time
        self.db.add_ocr_correction(rid, 0, orig, f"B_{suffix}", 1, 1)

        result = self.db.get_ocr_corrections_map(min_count=1)
        # "A" has highest count (3) → should be the winner
        self.assertEqual(result.get(orig), f"A_{suffix}")
        self.db.delete_receipt(rid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. Integration: Receipt Service Wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReceiptServiceWiring(unittest.TestCase):
    """Verify Smart OCR components are properly wired in receipt_service."""

    def _fresh_db(self):
        from app.database import Database
        return Database()

    def test_update_item_records_correction(self):
        """Updating an item should trigger correction recording."""
        fresh_db = self._fresh_db()

        rid = fresh_db.create_receipt("TEST-WIRE-001")
        fresh_db.add_receipt_items(rid, [
            {"code": "TEWI", "product": "Misread", "quantity": 3,
             "unit": "Piece", "confidence": 0.8, "unit_price": 0, "line_total": 0},
        ])
        receipt = fresh_db.get_receipt(rid)
        item_id = receipt["items"][0]["id"]

        from app.services.receipt_service import ReceiptService
        svc = ReceiptService()
        svc.db = fresh_db  # use fresh DB (avoids shutdown singleton issue)
        # Fix the misread: TEWI → TEW1
        svc.update_receipt_item(item_id, "TEW1", "Corrected", 3)

        # Verify correction was recorded
        corrections = fresh_db.get_ocr_corrections_map(min_count=1)
        self.assertEqual(corrections.get("TEWI"), "TEW1")

        fresh_db.delete_receipt(rid)

    def test_update_item_no_change_no_correction(self):
        """Updating with same values should NOT record correction."""
        fresh_db = self._fresh_db()

        rid = fresh_db.create_receipt("TEST-WIRE-002")
        fresh_db.add_receipt_items(rid, [
            {"code": "ABC", "product": "Paint", "quantity": 2,
             "unit": "Piece", "confidence": 0.9, "unit_price": 0, "line_total": 0},
        ])
        receipt = fresh_db.get_receipt(rid)
        item_id = receipt["items"][0]["id"]

        stats_before = fresh_db.get_ocr_correction_stats()
        total_before = stats_before["total_corrections"]

        from app.services.receipt_service import ReceiptService
        svc = ReceiptService()
        svc.db = fresh_db
        svc.update_receipt_item(item_id, "ABC", "Paint", 2)

        stats_after = fresh_db.get_ocr_correction_stats()
        self.assertEqual(stats_after["total_corrections"], total_before)

        fresh_db.delete_receipt(rid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. API Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSmartOCREndpoints(unittest.TestCase):
    """Test /api/corrections and /api/item-stats endpoints."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.main import app
        cls.client = TestClient(app)

    def test_corrections_endpoint(self):
        """GET /api/corrections should return stats and map."""
        resp = self.client.get("/api/corrections")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("stats", data)
        self.assertIn("active_corrections", data)
        self.assertIn("description", data)
        self.assertIsInstance(data["stats"]["total_corrections"], int)
        self.assertIsInstance(data["active_corrections"], dict)

    def test_item_stats_endpoint(self):
        """GET /api/item-stats should return product stats."""
        resp = self.client.get("/api/item-stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("product_stats", data)
        self.assertIn("total_products_with_history", data)
        self.assertIsInstance(data["product_stats"], dict)
        self.assertIsInstance(data["total_products_with_history"], int)


if __name__ == "__main__":
    unittest.main()
