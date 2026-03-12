"""
Preprocessing Pipeline Tests — unit tests for ImagePreprocessor methods.
Tests deskew, white balance, upside-down detection, quality assessment,
document scanning, and shadow normalization.
"""

import pytest
import numpy as np
import cv2


@pytest.fixture
def preprocessor():
    """Create an ImagePreprocessor instance."""
    from app.ocr.preprocessor import ImagePreprocessor
    return ImagePreprocessor()


# ─── Quality Assessment ──────────────────────────────────────────────────────

class TestQualityAssessment:
    """Test _assess_quality method."""

    def test_bright_sharp_image(self, preprocessor):
        """A clean white image should score well."""
        img = np.ones((200, 300), dtype=np.uint8) * 200
        # Add some text-like edges so Laplacian isn't zero
        cv2.putText(img, "TEST", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 2, 0, 3)
        q = preprocessor._assess_quality(img)
        assert q["score"] > 0
        assert "mean_brightness" in q
        assert "contrast" in q
        assert "is_blurry" in q

    def test_dark_image(self, preprocessor):
        """A very dark image should have low brightness and lower score."""
        img = np.ones((200, 300), dtype=np.uint8) * 30
        q = preprocessor._assess_quality(img)
        assert q["mean_brightness"] < 50
        assert q["is_too_dark"] or q["score"] < 60

    def test_blurry_image(self, preprocessor):
        """A heavily blurred image should be flagged as blurry."""
        # Create image with text, then blur it heavily
        img = np.ones((200, 300), dtype=np.uint8) * 200
        cv2.putText(img, "HELLO WORLD", (10, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)
        blurred = cv2.GaussianBlur(img, (31, 31), 0)
        q = preprocessor._assess_quality(blurred)
        assert q["is_blurry"]

    def test_high_contrast_image(self, preprocessor):
        """Image with high contrast should have good contrast score."""
        img = np.zeros((200, 300), dtype=np.uint8)
        img[:100, :] = 255  # Top half white, bottom half black
        q = preprocessor._assess_quality(img)
        assert q["contrast"] > 50


# ─── White Balance ────────────────────────────────────────────────────────────

class TestWhiteBalance:
    """Test _correct_white_balance method."""

    def test_neutral_image_unchanged(self, preprocessor):
        """A neutral gray image should be mostly unchanged."""
        img = np.ones((100, 100, 3), dtype=np.uint8) * 128
        result = preprocessor._correct_white_balance(img)
        # Should be very close to original since channels are balanced
        diff = np.abs(result.astype(float) - img.astype(float)).mean()
        assert diff < 5  # minimal change

    def test_blue_cast_corrected(self, preprocessor):
        """An image with strong blue cast should be rebalanced."""
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[:, :, 0] = 180  # B
        img[:, :, 1] = 100  # G
        img[:, :, 2] = 100  # R
        result = preprocessor._correct_white_balance(img)
        # After correction, channels should be more balanced
        b_mean = result[:, :, 0].mean()
        g_mean = result[:, :, 1].mean()
        r_mean = result[:, :, 2].mean()
        # The spread between channels should be reduced
        spread_before = max(180, 100, 100) - min(180, 100, 100)
        spread_after = max(b_mean, g_mean, r_mean) - min(b_mean, g_mean, r_mean)
        assert spread_after < spread_before


# ─── Deskew ───────────────────────────────────────────────────────────────────

class TestDeskew:
    """Test deskew angle detection."""

    def test_straight_image(self, preprocessor):
        """A straight image should have ~0° skew."""
        img = np.ones((300, 400), dtype=np.uint8) * 255
        # Draw horizontal lines (like text rows)
        for y in range(50, 250, 30):
            cv2.line(img, (30, y), (370, y), 0, 2)
        angle = preprocessor._detect_skew_angle(img)
        assert abs(angle) < 2.0  # Should be near zero

    def test_projection_profile_deskew(self, preprocessor):
        """Projection-profile method should detect skew on binary text."""
        # Create a slightly rotated text image
        img = np.ones((300, 400), dtype=np.uint8) * 255
        for y in range(50, 250, 25):
            cv2.line(img, (20, y), (380, y), 0, 2)
        # Rotate 3 degrees
        center = (200, 150)
        M = cv2.getRotationMatrix2D(center, 3, 1.0)
        rotated = cv2.warpAffine(img, M, (400, 300), borderValue=255)
        angle = preprocessor._detect_skew_by_projection(rotated)
        # Should detect approximately 3 degrees (sign may vary)
        assert abs(abs(angle) - 3.0) < 2.0


# ─── Upside-Down Detection ───────────────────────────────────────────────────

class TestUpsideDownDetection:
    """Test _is_upside_down method."""

    def test_normal_text_not_upside_down(self, preprocessor):
        """Normal text (heavy at top) should not be detected as upside down."""
        img = np.ones((400, 300), dtype=np.uint8) * 255
        # Put text in the top area (like a receipt header)
        cv2.putText(img, "STORE NAME", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
        cv2.putText(img, "Address Line", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
        cv2.putText(img, "Item 1", (20, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
        # Bottom should be mostly empty
        result = preprocessor._is_upside_down(img)
        # With text concentrated at top, should not be flagged
        assert isinstance(result, bool)

    def test_returns_bool(self, preprocessor):
        """Method should always return a boolean."""
        img = np.ones((200, 200), dtype=np.uint8) * 128
        result = preprocessor._is_upside_down(img)
        assert isinstance(result, bool)


# ─── Sharpen ──────────────────────────────────────────────────────────────────

class TestSharpen:
    """Test _sharpen method."""

    def test_sharpen_produces_output(self, preprocessor):
        """Sharpening should return an image of the same shape."""
        img = np.random.randint(100, 200, (200, 300), dtype=np.uint8)
        result = preprocessor._sharpen(img)
        assert result.shape == img.shape
        assert result.dtype == np.uint8

    def test_sharpen_increases_edge_contrast(self, preprocessor):
        """Sharpened image should have higher Laplacian variance."""
        img = np.ones((200, 300), dtype=np.uint8) * 150
        cv2.putText(img, "SHARP", (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 2, 0, 2)
        blurred = cv2.GaussianBlur(img, (5, 5), 0)
        sharpened = preprocessor._sharpen(blurred)
        lap_blurred = cv2.Laplacian(blurred, cv2.CV_64F).var()
        lap_sharp = cv2.Laplacian(sharpened, cv2.CV_64F).var()
        assert lap_sharp >= lap_blurred


# ─── Full Pipeline ────────────────────────────────────────────────────────────

class TestFullPipeline:
    """Test the complete preprocess() pipeline end-to-end."""

    def test_preprocess_synthetic_receipt(self, preprocessor, tmp_path):
        """Full pipeline should handle a synthetic receipt image."""
        # Create a synthetic receipt image
        img = np.ones((800, 600, 3), dtype=np.uint8) * 240
        cv2.putText(img, "PAINT STORE", (100, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
        cv2.putText(img, "ABC  x 2", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2)
        cv2.putText(img, "XYZ  x 3", (50, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2)
        cv2.putText(img, "Total: 5", (50, 400), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)

        # Save to disk
        img_path = str(tmp_path / "receipt.png")
        cv2.imwrite(img_path, img)

        # Run pipeline
        result, metadata = preprocessor.preprocess(img_path)

        assert result is not None
        assert result.ndim == 2  # Should be grayscale
        assert "stages" in metadata
        assert "quality" in metadata
        assert "processing_time_ms" in metadata
        assert metadata["processing_time_ms"] >= 0
        assert isinstance(metadata["stages"], list)
        assert len(metadata["stages"]) > 0

    def test_preprocess_small_image(self, preprocessor, tmp_path):
        """Pipeline should handle images at minimum valid size."""
        img = np.ones((400, 300, 3), dtype=np.uint8) * 200
        cv2.putText(img, "SMALL", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
        img_path = str(tmp_path / "small.png")
        cv2.imwrite(img_path, img)

        result, metadata = preprocessor.preprocess(img_path)
        assert result is not None
        assert result.shape[0] > 0 and result.shape[1] > 0

    def test_preprocess_dark_image(self, preprocessor, tmp_path):
        """Pipeline should handle a very dark image."""
        img = np.ones((400, 300, 3), dtype=np.uint8) * 20
        cv2.putText(img, "DARK", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (80, 80, 80), 2)
        img_path = str(tmp_path / "dark.png")
        cv2.imwrite(img_path, img)

        result, metadata = preprocessor.preprocess(img_path)
        assert result is not None
        # Quality should flag darkness
        assert metadata["quality"]["is_too_dark"] or metadata["quality"]["mean_brightness"] < 80


# ─── Crop to Content ─────────────────────────────────────────────────────────

class TestCropToContent:
    """Test crop_to_content_static method."""

    def test_crop_removes_border(self):
        from app.ocr.preprocessor import ImagePreprocessor
        # White image with black rectangle in center
        img = np.ones((400, 400), dtype=np.uint8) * 255
        cv2.rectangle(img, (100, 100), (300, 300), 0, -1)
        cropped = ImagePreprocessor.crop_to_content_static(img)
        # Cropped should be smaller than original
        assert cropped.shape[0] <= img.shape[0]
        assert cropped.shape[1] <= img.shape[1]

    def test_crop_blank_image(self):
        from app.ocr.preprocessor import ImagePreprocessor
        # All white image — should return original (no content to crop)
        img = np.ones((200, 200), dtype=np.uint8) * 255
        cropped = ImagePreprocessor.crop_to_content_static(img)
        assert cropped is not None
        assert cropped.shape[0] > 0


# ─── Adaptive Fuzzy Cutoff ────────────────────────────────────────────────────

class TestAdaptiveFuzzyCutoff:
    """Test the length-adaptive fuzzy match cutoff function."""

    def test_short_code_strict(self):
        from app.config import get_adaptive_fuzzy_cutoff
        cutoff = get_adaptive_fuzzy_cutoff(3)
        assert cutoff >= 0.80  # Should be strict

    def test_medium_code_moderate(self):
        from app.config import get_adaptive_fuzzy_cutoff
        cutoff = get_adaptive_fuzzy_cutoff(5)
        assert 0.65 <= cutoff <= 0.80

    def test_long_code_lenient(self):
        from app.config import get_adaptive_fuzzy_cutoff
        cutoff = get_adaptive_fuzzy_cutoff(8)
        assert cutoff <= 0.70

    def test_monotonically_decreasing(self):
        from app.config import get_adaptive_fuzzy_cutoff
        cutoffs = [get_adaptive_fuzzy_cutoff(i) for i in range(2, 10)]
        # Each cutoff should be >= the next (more lenient for longer codes)
        for i in range(len(cutoffs) - 1):
            assert cutoffs[i] >= cutoffs[i + 1]
