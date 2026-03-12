"""
Tests for OCR accuracy improvements:
    - Confidence calibration (EasyOCR score adjustment)
    - Hybrid engine routing with calibrated confidence + catalog match rate
    - Azure model strategy defaults
    - Azure confidence defaults
"""

import pytest
from unittest.mock import patch, MagicMock


# ─── Confidence Calibration Tests ─────────────────────────────────────────────

class TestConfidenceCalibration:
    """Tests for OCREngine.calibrate_confidence() — ensures raw EasyOCR
    scores are properly adjusted for text quality indicators."""

    def setup_method(self):
        from app.ocr.engine import OCREngine
        self.calibrate = OCREngine.calibrate_confidence

    def test_empty_text_returns_zero(self):
        """Empty or whitespace-only text should return 0.0 confidence."""
        assert self.calibrate("", 0.95) == 0.0
        assert self.calibrate("   ", 0.95) == 0.0

    def test_single_char_penalty(self):
        """Single-character detections should be heavily penalized."""
        raw = 0.90
        cal = self.calibrate("A", raw)
        assert cal < raw * 0.65  # At least 35% penalty
        assert cal > 0.0

    def test_two_char_penalty(self):
        """Two-character detections should receive moderate penalty."""
        raw = 0.90
        cal = self.calibrate("AB", raw)
        assert cal < raw * 0.85  # At least 15% penalty
        assert cal > self.calibrate("A", raw)  # Less penalty than single char

    def test_clean_product_code_minimal_penalty(self):
        """Clean alphanumeric product codes (3-7 chars) should keep high confidence."""
        raw = 0.90
        assert self.calibrate("TEW1", raw) >= raw * 0.90
        assert self.calibrate("PEPW20", raw) >= raw * 0.90
        assert self.calibrate("ABC", raw) >= raw * 0.90
        assert self.calibrate("GHI", raw) >= raw * 0.90

    def test_noise_chars_penalty(self):
        """Text with many OCR noise characters should be penalized."""
        raw = 0.85
        # >30% noise chars
        cal = self.calibrate("[|{}", raw)
        assert cal < raw * 0.70
        # >15% noise chars
        cal2 = self.calibrate("AB|C", raw)
        assert cal2 < raw * 0.85

    def test_repetitive_text_penalty(self):
        """Repetitive character patterns should be penalized (OCR artifacts)."""
        raw = 0.88
        cal = self.calibrate("IIII", raw)
        assert cal < raw * 0.75
        cal2 = self.calibrate("0000", raw)
        assert cal2 < raw * 0.75

    def test_long_digit_string_penalty(self):
        """Long digit-only strings should be penalized (likely misreads)."""
        raw = 0.85
        cal = self.calibrate("12345678", raw)
        assert cal < raw * 0.80

    def test_never_exceeds_raw(self):
        """Calibrated confidence should never exceed raw confidence."""
        texts = ["ABC", "TEW1", "PEPW20", "Hello World", "A", "|||"]
        for text in texts:
            for raw in [0.3, 0.5, 0.7, 0.9, 1.0]:
                cal = self.calibrate(text, raw)
                assert cal <= raw, f"Calibrated {cal} > raw {raw} for '{text}'"

    def test_mixed_symbols_short_text_penalty(self):
        """Short text with multiple symbols should be penalized."""
        raw = 0.80
        cal = self.calibrate("J1L{2", raw)
        assert cal < raw * 0.80

    def test_returns_float(self):
        """Result should always be a float rounded to 4 decimal places."""
        result = self.calibrate("ABC", 0.87654321)
        assert isinstance(result, float)
        # Should be rounded to 4 decimal places
        assert result == round(result, 4)

    def test_zero_raw_confidence(self):
        """Zero raw confidence should stay zero regardless of text."""
        assert self.calibrate("PerfectText", 0.0) == 0.0


# ─── Config Defaults Tests ───────────────────────────────────────────────────

class TestConfigDefaults:
    """Verify config defaults have been updated for accuracy improvements."""

    def test_azure_model_strategy_default(self):
        """Default Azure model should be receipt-only (structured extraction)."""
        import importlib
        import app.config
        importlib.reload(app.config)
        # When AZURE_MODEL_STRATEGY env var is not set, default should be "receipt-only"
        with patch.dict("os.environ", {}, clear=False):
            # Remove env override if present
            import os
            orig = os.environ.pop("AZURE_MODEL_STRATEGY", None)
            try:
                importlib.reload(app.config)
                assert app.config.AZURE_MODEL_STRATEGY == "receipt-only"
            finally:
                if orig is not None:
                    os.environ["AZURE_MODEL_STRATEGY"] = orig
                importlib.reload(app.config)

    def test_skip_threshold_raised(self):
        """LOCAL_CONFIDENCE_SKIP_THRESHOLD should be >= 0.85."""
        from app.config import LOCAL_CONFIDENCE_SKIP_THRESHOLD
        assert LOCAL_CONFIDENCE_SKIP_THRESHOLD >= 0.85

    def test_catalog_match_threshold_exists(self):
        """LOCAL_CATALOG_MATCH_SKIP_THRESHOLD config should exist."""
        from app.config import LOCAL_CATALOG_MATCH_SKIP_THRESHOLD
        assert 0.0 < LOCAL_CATALOG_MATCH_SKIP_THRESHOLD <= 1.0


# ─── Hybrid Engine Routing Tests ─────────────────────────────────────────────

class TestHybridRouting:
    """Tests for the hybrid engine's improved routing logic."""

    def test_calibrated_avg_confidence(self):
        """_calibrated_avg_confidence should return lower values than raw avg."""
        from app.ocr.hybrid_engine import HybridOCREngine

        engine = HybridOCREngine.__new__(HybridOCREngine)

        # Detections with short/noisy text that EasyOCR would overestimate
        detections = [
            {"text": "A", "confidence": 0.90},      # Single char → heavy penalty
            {"text": "|{", "confidence": 0.85},      # Noise chars
            {"text": "IIII", "confidence": 0.88},    # Repetitive
            {"text": "TEW1", "confidence": 0.92},    # Clean product code
        ]

        raw_avg = engine._avg_confidence(detections)
        cal_avg = engine._calibrated_avg_confidence(detections)

        assert cal_avg < raw_avg, (
            f"Calibrated {cal_avg} should be less than raw {raw_avg} "
            f"for noisy detections"
        )

    def test_calibrated_avg_clean_text(self):
        """Clean product code detections should have minimal calibration penalty."""
        from app.ocr.hybrid_engine import HybridOCREngine

        engine = HybridOCREngine.__new__(HybridOCREngine)

        clean_detections = [
            {"text": "TEW1", "confidence": 0.92},
            {"text": "PEPW20", "confidence": 0.90},
            {"text": "ABC 5", "confidence": 0.88},
            {"text": "GHI 3", "confidence": 0.91},
        ]

        raw_avg = engine._avg_confidence(clean_detections)
        cal_avg = engine._calibrated_avg_confidence(clean_detections)

        # Clean text should have small penalty (within 15%)
        assert cal_avg >= raw_avg * 0.85, (
            f"Calibrated {cal_avg} should be close to raw {raw_avg} "
            f"for clean product codes"
        )

    def test_catalog_match_rate_with_catalog(self):
        """Catalog match rate should detect known product codes."""
        from app.ocr.hybrid_engine import HybridOCREngine

        engine = HybridOCREngine.__new__(HybridOCREngine)

        detections = [
            {"text": "ABC 5", "confidence": 0.90},
            {"text": "XYZ 3", "confidence": 0.88},
            {"text": "TOTAL", "confidence": 0.92},
        ]

        # Mock the catalog lookup — the import is inside _catalog_match_rate
        mock_catalog = {"ABC": "Paint A", "XYZ": "Paint B", "PQR": "Primer"}

        mock_receipt_service = MagicMock()
        mock_receipt_service.parser.product_catalog = mock_catalog
        with patch.dict("sys.modules", {}):
            with patch("app.services.receipt_service.receipt_service", mock_receipt_service):
                rate = engine._catalog_match_rate(detections)
                # ABC and XYZ should match, TOTAL should not
                assert rate > 0.5  # At least 2/3 matched

    def test_catalog_match_rate_empty(self):
        """Empty detections should return 0.0 match rate."""
        from app.ocr.hybrid_engine import HybridOCREngine

        engine = HybridOCREngine.__new__(HybridOCREngine)
        assert engine._catalog_match_rate([]) == 0.0


# ─── Azure Engine Confidence Tests ───────────────────────────────────────────

class TestAzureConfidenceDefaults:
    """Tests for Azure engine confidence score defaults."""

    def test_read_model_default_confidence_not_inflated(self):
        """Azure Read model default confidence should be 0.80, not 0.95."""
        from app.ocr.azure_engine import AzureOCREngine

        engine = AzureOCREngine.__new__(AzureOCREngine)

        # Mock Azure result with lines but no word-level confidences
        mock_page = MagicMock()
        mock_line = MagicMock()
        mock_line.content = "TEW1 5"
        mock_line.polygon = [0, 0, 100, 0, 100, 20, 0, 20]
        mock_page.lines = [mock_line]
        mock_page.words = []  # No word-level confidence available

        mock_result = MagicMock()
        mock_result.pages = [mock_page]

        detections = engine._convert_read_to_detections(mock_result)

        assert len(detections) == 1
        # Default should be 0.80, NOT 0.95
        assert detections[0]["confidence"] == 0.80

    def test_page_text_default_confidence_not_inflated(self):
        """_extract_page_text default confidence should be 0.80, not 1.0."""
        from app.ocr.azure_engine import AzureOCREngine

        engine = AzureOCREngine.__new__(AzureOCREngine)

        mock_page = MagicMock()
        mock_line = MagicMock()
        mock_line.content = "ABC 3"
        mock_line.polygon = [0, 0, 100, 0, 100, 20, 0, 20]
        mock_page.lines = [mock_line]
        mock_page.words = []

        mock_result = MagicMock()
        mock_result.pages = [mock_page]

        detections = engine._extract_page_text(mock_result)

        assert len(detections) == 1
        assert detections[0]["confidence"] == 0.80


# ─── Integration Tests ──────────────────────────────────────────────────────

class TestAccuracyIntegration:
    """End-to-end tests verifying the accuracy improvements work together."""

    def test_garbled_text_gets_low_calibrated_confidence(self):
        """Garbled OCR output should have much lower calibrated confidence."""
        from app.ocr.engine import OCREngine

        # Simulate typical EasyOCR output on poorly-read handwritten text
        garbled_detections = [
            {"text": "|", "confidence": 0.82},       # Single pipe
            {"text": "6H[", "confidence": 0.78},      # Noise chars
            {"text": "IIII", "confidence": 0.85},     # Repetitive
            {"text": "0000", "confidence": 0.80},     # Repetitive digits
            {"text": "}", "confidence": 0.75},        # Single brace
        ]

        # Raw average would be ~0.80
        raw_avg = sum(d["confidence"] for d in garbled_detections) / len(garbled_detections)

        # Calibrated average should be significantly lower
        cal_confs = [
            OCREngine.calibrate_confidence(d["text"], d["confidence"])
            for d in garbled_detections
        ]
        cal_avg = sum(cal_confs) / len(cal_confs)

        assert cal_avg < 0.55, (
            f"Garbled text calibrated avg {cal_avg:.3f} should be < 0.55 "
            f"(raw was {raw_avg:.3f})"
        )
        # This would now fail the 0.85 skip threshold → route to Azure ✓

    def test_clean_receipt_keeps_high_calibrated_confidence(self):
        """Well-read receipt text should maintain high calibrated confidence."""
        from app.ocr.engine import OCREngine

        clean_detections = [
            {"text": "TEW1 5", "confidence": 0.92},
            {"text": "PEPW20 3", "confidence": 0.90},
            {"text": "ABC 2", "confidence": 0.88},
            {"text": "TOTAL 10", "confidence": 0.91},
        ]

        cal_confs = [
            OCREngine.calibrate_confidence(d["text"], d["confidence"])
            for d in clean_detections
        ]
        cal_avg = sum(cal_confs) / len(cal_confs)

        assert cal_avg >= 0.80, (
            f"Clean receipt calibrated avg {cal_avg:.3f} should be >= 0.80"
        )
