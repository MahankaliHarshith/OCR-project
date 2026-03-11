"""
Image Preprocessing Module using OpenCV.
Handles image enhancement, perspective correction, and quality validation
to optimize receipt images for OCR processing.
"""

import cv2
import numpy as np
import logging
import time
from pathlib import Path
from typing import Tuple, Optional, Dict
from PIL import Image, ExifTags

from app.config import (
    GAUSSIAN_BLUR_KERNEL,
    ADAPTIVE_THRESH_BLOCK_SIZE,
    ADAPTIVE_THRESH_C,
    CLAHE_CLIP_LIMIT,
    CLAHE_TILE_GRID_SIZE,
    IMAGE_MAX_DIMENSION,
    IMAGE_MIN_WIDTH,
    IMAGE_MIN_HEIGHT,
)

logger = logging.getLogger(__name__)


class ImagePreprocessor:
    """
    Preprocesses receipt images for optimal OCR accuracy.

    Pipeline:
        Raw Image → Grayscale → Gaussian Blur → Adaptive Threshold
        → Edge Detection → Perspective Transform → CLAHE → Enhanced Image
    """

    def __init__(self):
        self.clahe = cv2.createCLAHE(
            clipLimit=CLAHE_CLIP_LIMIT,
            tileGridSize=CLAHE_TILE_GRID_SIZE,
        )

    def preprocess(self, image_path: str) -> Tuple[np.ndarray, Dict]:
        """
        Complete preprocessing pipeline for a receipt image.
        Optimized for HANDWRITTEN text on paper — avoids aggressive
        binarization that destroys ink strokes.

        Args:
            image_path: Path to the input image file.

        Returns:
            Tuple of (processed image array, processing metadata dict).
        """
        start_time = time.time()
        metadata = {"stages": [], "warnings": []}

        logger.debug(f"Starting preprocessing pipeline for: {image_path}")

        # 1. Load image
        img = self._load_image(image_path)
        metadata["original_size"] = (img.shape[1], img.shape[0])
        logger.debug(f"  [1/6] Image loaded: {img.shape[1]}x{img.shape[0]}, channels={img.shape[2] if len(img.shape) > 2 else 1}")

        # 2. Resize if too large
        img = self._resize_if_needed(img)
        metadata["stages"].append("resize")
        metadata["_color_image"] = img.copy()   # keep resized color for OCR color pass
        logger.debug(f"  [2/6] Resized to: {img.shape[1]}x{img.shape[0]}")

        # 3. Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        metadata["stages"].append("grayscale")
        logger.debug("  [3/6] Converted to grayscale")

        # 3a. Deskew: detect and correct small rotation (<15°)
        # For same-type receipts: only deskew if angle is significant (> 1.5°)
        # to avoid unnecessary processing on well-aligned photos
        skew_angle = self._detect_skew_angle(gray)
        if abs(skew_angle) > 1.5:  # Higher threshold: skip minor rotations for speed
            gray = self._rotate_image(gray, -skew_angle)
            # Also rotate the color image so perspective correction uses aligned version
            img = self._rotate_image(img, -skew_angle)
            metadata["stages"].append("deskew")
            logger.debug(f"  [3a/6] Deskewed by {skew_angle:.1f}°")
        elif abs(skew_angle) > 0.5:
            logger.debug(f"  [3a/6] Minor skew {skew_angle:.1f}° detected but below correction threshold")

        # 4. Quality assessment
        quality = self._assess_quality(gray)
        metadata["quality"] = quality
        logger.debug(
            f"  [4/6] Quality: score={quality['score']:.1f}, "
            f"blurry={quality['is_blurry']}, brightness={quality['mean_brightness']:.0f}, "
            f"contrast={quality['contrast']:.1f}"
        )
        if quality["is_blurry"]:
            metadata["warnings"].append("Image appears blurry.")
            logger.warning(f"  ⚠ Image is blurry (laplacian_var={quality['laplacian_variance']:.1f})")

        # 5. GENTLE enhancement for handwriting (no aggressive binarization)
        # a) Light Gaussian blur to reduce camera noise only
        denoised = cv2.GaussianBlur(gray, GAUSSIAN_BLUR_KERNEL, 0)
        metadata["stages"].append("light_denoise")
        logger.debug(f"  [5/6] Light Gaussian blur (kernel={GAUSSIAN_BLUR_KERNEL})")

        # b) If image is blurry, apply sharpening to recover ink edges
        if quality["is_blurry"]:
            denoised = self._sharpen(denoised)
            metadata["stages"].append("sharpening")
            logger.debug("  [5a/6] Sharpening applied (blurry image)")

        # c) Bilateral filter to reduce noise while preserving ink edges
        #    Only on noisy images (low quality score + not already blurry-sharpened)
        if quality["score"] < 40 and not quality["is_blurry"]:
            denoised = cv2.bilateralFilter(denoised, 9, 75, 75)
            metadata["stages"].append("bilateral_filter")
            logger.debug("  [5a/6] Bilateral filter applied (noisy image)")

        # d) Morphological closing — only for BLURRY images with broken ink
        # Clear handwriting should NOT be morphed: closing merges adjacent chars
        # in tight codes like W1, W4, making them unreadable to OCR.
        if quality["is_blurry"]:
            morph_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            denoised = cv2.morphologyEx(denoised, cv2.MORPH_CLOSE, morph_kernel)
            metadata["stages"].append("morph_close")
            logger.debug("  [5c/6] Morphological closing (broken ink strokes — blurry image)")
        else:
            logger.debug("  [5c/6] Morphological closing SKIPPED (clear image — preserving char separation)")

        # e) CLAHE contrast enhancement — makes ink stand out from paper
        # Use stronger CLAHE for dark images to better separate ink from paper
        if quality.get('mean_brightness', 128) < 100:
            # Dark image: more aggressive clip limit to pull out ink detail
            dark_clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT * 1.5, tileGridSize=CLAHE_TILE_GRID_SIZE)
            enhanced = dark_clahe.apply(denoised)
            metadata["stages"].append("clahe_enhancement_strong")
            logger.debug(f"  [5e/6] Strong CLAHE for dark image (clip={CLAHE_CLIP_LIMIT * 1.5:.1f})")
        else:
            enhanced = self.clahe.apply(denoised)
            metadata["stages"].append("clahe_enhancement")
            logger.debug("  [5e/6] CLAHE contrast enhancement applied")

        # f) Shadow / gradient illumination normalization
        # Divides by a heavily-blurred background estimate to flatten uneven
        # lighting caused by phone flash gradients, corner shadows, or lamp angle.
        # Only applied when illumination is TRULY uneven (background std-dev > 15)
        # AND when it measurably increases contrast (std-dev check).
        # Skipped on uniformly-lit images where it would flatten thin pen strokes.
        try:
            _bg = cv2.GaussianBlur(enhanced, (51, 51), 0).astype(np.float32) + 1.0
            _bg_std = float(np.std(_bg))
            if _bg_std > 15:  # Uneven illumination detected
                _norm = np.clip(enhanced.astype(np.float32) / _bg * 128.0, 0, 255).astype(np.uint8)
                if _norm.std() >= enhanced.std():
                    enhanced = _norm
                    metadata["stages"].append("shadow_normalize")
                    logger.debug(f"  [5f/6] Shadow normalization applied (bg_std={_bg_std:.1f})")
                else:
                    logger.debug(f"  [5f/6] Shadow normalization skipped (no std-dev gain, bg_std={_bg_std:.1f})")
            else:
                logger.debug(f"  [5f/6] Shadow normalization skipped (uniform illumination, bg_std={_bg_std:.1f})")
        except Exception as _sn_err:
            logger.debug(f"  [5f/6] Shadow normalization error (skipped): {_sn_err}")

        # g) Brightness normalization — handle shadows / uneven lighting
        mean_val = np.mean(enhanced)
        contrast_val = quality.get('contrast', np.std(enhanced))
        if mean_val < 100:
            # Very dark image — aggressive brighten with higher alpha
            alpha = 1.6
            beta = 60
            enhanced = cv2.convertScaleAbs(enhanced, alpha=alpha, beta=beta)
            metadata["stages"].append("brightness_correction_strong")
            logger.debug(f"  [5g/6] Strong brightness correction (very dark image, mean={mean_val:.0f})")
        elif mean_val < 120:
            # Moderately dark — standard brighten
            alpha = 1.4  # Contrast
            beta = 40    # Brightness
            enhanced = cv2.convertScaleAbs(enhanced, alpha=alpha, beta=beta)
            metadata["stages"].append("brightness_correction")
            logger.debug(f"  [5g/6] Brightness correction (dark image, mean={mean_val:.0f})")
        elif mean_val > 200:
            # Image is washed out — increase contrast
            alpha = 1.6
            beta = -30
            enhanced = cv2.convertScaleAbs(enhanced, alpha=alpha, beta=beta)
            metadata["stages"].append("contrast_correction")
            logger.debug(f"  [5g/6] Contrast correction (bright image, mean={mean_val:.0f})")

        # g2) Low-contrast boost: if ink and paper have similar intensity,
        # stretch the histogram to separate them for better OCR recognition.
        if contrast_val < 30:
            # Histogram equalization just on the ink-dominant region
            p_low, p_high = np.percentile(enhanced, [5, 95])
            if p_high - p_low > 10:  # There IS some contrast, just weak
                enhanced = np.clip(
                    (enhanced.astype(np.float32) - p_low) * 255.0 / (p_high - p_low),
                    0, 255
                ).astype(np.uint8)
                metadata["stages"].append("contrast_stretch")
                logger.debug(f"  [5g2/6] Contrast stretch applied (contrast={contrast_val:.1f})")

        # 6. Try perspective correction (optional, often skipped)
        corrected = self._perspective_correct(enhanced, img)
        if corrected is not None:
            enhanced = corrected
            metadata["stages"].append("perspective_correction")
            logger.debug("  [6/6] Perspective correction applied")
        else:
            logger.debug("  [6/6] Perspective correction skipped")

        elapsed_ms = int((time.time() - start_time) * 1000)
        metadata["processing_time_ms"] = elapsed_ms
        metadata["processed_size"] = (enhanced.shape[1], enhanced.shape[0])

        logger.info(
            f"Image preprocessed in {elapsed_ms}ms | "
            f"Stages: {len(metadata['stages'])} | "
            f"Quality score: {quality['score']:.1f}"
        )

        return enhanced, metadata

    def detect_grid_structure(self, gray_image: np.ndarray) -> bool:
        """
        Detect if the image has a grid/table structure (boxed template receipt).
        Uses morphological operations to find strong horizontal and vertical lines.
        Returns True if a significant grid structure is found.
        """
        h, w = gray_image.shape[:2]
        start = time.time()

        # Binarize (invert so lines are white)
        _, binary = cv2.threshold(
            gray_image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )

        # Detect long horizontal lines
        h_kernel_len = max(w // 5, 50)
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_kernel_len, 1))
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=2)

        # Detect long vertical lines
        v_kernel_len = max(h // 5, 50)
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_kernel_len))
        vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel, iterations=2)

        # Count significant horizontal lines (span ≥40% of image width)
        h_contours, _ = cv2.findContours(
            horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        sig_h = sum(1 for c in h_contours if cv2.boundingRect(c)[2] > w * 0.4)

        # Count significant vertical lines (span ≥30% of image height)
        v_contours, _ = cv2.findContours(
            vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        sig_v = sum(1 for c in v_contours if cv2.boundingRect(c)[3] > h * 0.3)

        # Need strong evidence of a table: many rows (≥6 h-lines) and
        # multiple columns (≥3 v-lines) to avoid false positives on
        # ruled notebook paper or free-form handwriting
        is_structured = sig_h >= 6 and sig_v >= 3
        elapsed_ms = int((time.time() - start) * 1000)
        logger.debug(
            f"Grid detection: {sig_h} h-lines, {sig_v} v-lines → "
            f"structured={is_structured} ({elapsed_ms}ms)"
        )
        return is_structured

    def preprocess_for_display(self, image_path: str) -> np.ndarray:
        """
        Light preprocessing for display/preview purposes only.

        Args:
            image_path: Path to the image file.

        Returns:
            Lightly processed image suitable for display.
        """
        img = self._load_image(image_path)
        img = self._resize_if_needed(img, max_dim=1200)
        return img

    def _load_image(self, image_path: str) -> np.ndarray:
        """Load and validate an image file, correcting EXIF orientation."""
        path = Path(image_path)
        if not path.exists():
            logger.error(f"Image file not found: {image_path}")
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Auto-correct EXIF orientation (phone cameras embed rotation metadata)
        img = self._load_with_exif_correction(str(path))

        h, w = img.shape[:2]
        file_size_kb = path.stat().st_size / 1024
        logger.debug(f"Image loaded: {path.name} | {w}x{h} | {file_size_kb:.0f} KB")

        if w < IMAGE_MIN_WIDTH // 2 or h < IMAGE_MIN_HEIGHT // 2:
            logger.error(f"Image too small: {w}x{h} (min: {IMAGE_MIN_WIDTH}x{IMAGE_MIN_HEIGHT})")
            raise ValueError(
                f"Image too small ({w}x{h}). "
                f"Minimum recommended: {IMAGE_MIN_WIDTH}x{IMAGE_MIN_HEIGHT}"
            )

        return img

    def _load_with_exif_correction(self, image_path: str) -> np.ndarray:
        """
        Load image and auto-rotate based on EXIF orientation tag.
        Phone cameras store landscape-oriented pixels + an EXIF flag to
        indicate how to rotate for display. cv2.imread ignores EXIF,
        producing 90°/180°/270° rotated images that destroy OCR accuracy.
        """
        try:
            pil_img = Image.open(image_path)
            # Find EXIF orientation tag
            exif_data = pil_img.getexif()
            orientation = None
            for tag_id, value in exif_data.items():
                if ExifTags.TAGS.get(tag_id) == 'Orientation':
                    orientation = value
                    break

            if orientation is not None and orientation != 1:
                rotate_map = {
                    2: [Image.FLIP_LEFT_RIGHT],
                    3: [Image.ROTATE_180],
                    4: [Image.FLIP_TOP_BOTTOM],
                    5: [Image.TRANSPOSE],
                    6: [Image.ROTATE_270],  # 90° CW
                    7: [Image.TRANSVERSE],
                    8: [Image.ROTATE_90],   # 90° CCW
                }
                ops = rotate_map.get(orientation, [])
                for op in ops:
                    pil_img = pil_img.transpose(op)
                logger.debug(f"EXIF orientation corrected: {orientation} → upright")

            # Convert PIL (RGB) to OpenCV (BGR)
            img_rgb = np.array(pil_img.convert('RGB'))
            img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
            return img_bgr
        except Exception as e:
            logger.debug(f"EXIF correction failed ({e}), falling back to cv2.imread")
            img = cv2.imread(image_path)
            if img is None:
                raise ValueError(f"Cannot read image: {image_path}")
            return img

    def _resize_if_needed(
        self, img: np.ndarray, max_dim: int = IMAGE_MAX_DIMENSION
    ) -> np.ndarray:
        """Resize image if any dimension exceeds the maximum."""
        h, w = img.shape[:2]
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            logger.debug(f"Image resized from {w}x{h} to {new_w}x{new_h}")
        return img

    def _assess_quality(self, gray: np.ndarray) -> Dict:
        """
        Assess image quality (blur, brightness, contrast).

        Args:
            gray: Grayscale image.

        Returns:
            Quality assessment dictionary.
        """
        # Blur detection using Laplacian variance
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        is_blurry = laplacian_var < 100

        # Brightness assessment
        mean_brightness = np.mean(gray)
        is_too_dark = mean_brightness < 60
        is_too_bright = mean_brightness > 220

        # Contrast assessment
        contrast = gray.std()
        is_low_contrast = contrast < 30

        # Overall score (0-100)
        score = min(100, laplacian_var / 10)
        if is_too_dark or is_too_bright:
            score *= 0.7
        if is_low_contrast:
            score *= 0.8

        return {
            "score": round(score, 1),
            "laplacian_variance": round(laplacian_var, 2),
            "is_blurry": is_blurry,
            "mean_brightness": round(mean_brightness, 1),
            "is_too_dark": is_too_dark,
            "is_too_bright": is_too_bright,
            "contrast": round(contrast, 1),
            "is_low_contrast": is_low_contrast,
        }

    def _perspective_correct(
        self, gray: np.ndarray, original: np.ndarray
    ) -> Optional[np.ndarray]:
        """
        Attempt to detect receipt edges and apply perspective correction.

        Args:
            gray: Grayscale blurred image.
            original: Original color image (for reference).

        Returns:
            Perspective-corrected grayscale image, or None if correction fails.
        """
        try:
            # Edge detection
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)

            # Find contours
            contours, _ = cv2.findContours(
                edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            if not contours:
                return None

            # Image area for minimum size check
            img_h, img_w = gray.shape[:2]
            img_area = img_h * img_w
            min_contour_area = img_area * 0.3  # Contour must cover >30% of image

            # Find the largest rectangular contour
            contours = sorted(contours, key=cv2.contourArea, reverse=True)

            for contour in contours[:5]:
                # Skip contours that are too small — these are NOT the receipt
                if cv2.contourArea(contour) < min_contour_area:
                    continue

                peri = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.02 * peri, True)

                if len(approx) == 4:
                    # Found a quadrilateral - apply perspective transform
                    pts = approx.reshape(4, 2).astype(np.float32)
                    rect = self._order_points(pts)

                    width = max(
                        np.linalg.norm(rect[0] - rect[1]),
                        np.linalg.norm(rect[2] - rect[3]),
                    )
                    height = max(
                        np.linalg.norm(rect[0] - rect[3]),
                        np.linalg.norm(rect[1] - rect[2]),
                    )

                    # Safety check: result must be at least 300×300
                    if width < 300 or height < 300:
                        logger.debug(
                            f"Perspective correction rejected: result too small "
                            f"({int(width)}x{int(height)})"
                        )
                        continue

                    # Safety check: reject warps that drastically change aspect ratio
                    # (e.g., portrait 955x1280 warped to near-square 723x737)
                    orig_ratio = img_w / img_h
                    new_ratio = width / height
                    ratio_change = abs(new_ratio - orig_ratio) / orig_ratio
                    if ratio_change > 0.3:
                        logger.debug(
                            f"Perspective correction rejected: aspect ratio change too large "
                            f"({orig_ratio:.2f} → {new_ratio:.2f}, {ratio_change:.0%} change)"
                        )
                        continue

                    dst = np.array(
                        [
                            [0, 0],
                            [width - 1, 0],
                            [width - 1, height - 1],
                            [0, height - 1],
                        ],
                        dtype=np.float32,
                    )

                    matrix = cv2.getPerspectiveTransform(rect, dst)
                    warped = cv2.warpPerspective(
                        gray, matrix, (int(width), int(height))
                    )

                    logger.debug("Perspective correction applied successfully.")
                    return warped

            return None

        except Exception as e:
            logger.warning(f"Perspective correction failed: {e}")
            return None

    def _order_points(self, pts: np.ndarray) -> np.ndarray:
        """Order 4 points as: top-left, top-right, bottom-right, bottom-left."""
        rect = np.zeros((4, 2), dtype=np.float32)
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]  # top-left
        rect[2] = pts[np.argmax(s)]  # bottom-right

        diff = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(diff)]  # top-right
        rect[3] = pts[np.argmax(diff)]  # bottom-left

        return rect

    def _sharpen(self, image: np.ndarray) -> np.ndarray:
        """Apply unsharp masking to sharpen the image."""
        gaussian = cv2.GaussianBlur(image, (0, 0), 3)
        sharpened = cv2.addWeighted(image, 1.5, gaussian, -0.5, 0)
        return sharpened

    def _detect_skew_angle(self, gray: np.ndarray) -> float:
        """
        Detect skew angle of text lines using Hough line transform.
        Returns the dominant angle in degrees (positive = clockwise tilt).
        Only returns angles < 15° to avoid false corrections on unusual layouts.
        """
        h, w = gray.shape[:2]

        # Edge detection on thresholded image
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        edges = cv2.Canny(binary, 50, 150, apertureSize=3)

        # Detect lines using probabilistic Hough transform
        lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 720,  # 0.25° resolution
            threshold=100,
            minLineLength=w // 4,  # Lines must span ≥25% of image width
            maxLineGap=20,
        )

        if lines is None or len(lines) < 3:
            return 0.0

        # Calculate angle of each line
        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            dx = x2 - x1
            dy = y2 - y1
            if abs(dx) < 10:  # Skip near-vertical lines
                continue
            angle = np.degrees(np.arctan2(dy, dx))
            # Only consider near-horizontal lines (within ±15°)
            if abs(angle) <= 15:
                angles.append(angle)

        if not angles:
            return 0.0

        # Use median angle (robust against outliers)
        median_angle = float(np.median(angles))

        # Only correct if there's clear consensus (low std dev)
        angle_std = float(np.std(angles))
        if angle_std > 5.0:
            logger.debug(f"  Skew detection: angle={median_angle:.1f}° but std={angle_std:.1f}° (too noisy, skipping)")
            return 0.0

        logger.debug(f"  Skew detection: {median_angle:.1f}° (from {len(angles)} lines, std={angle_std:.1f}°)")
        return median_angle

    def _rotate_image(self, image: np.ndarray, angle: float) -> np.ndarray:
        """
        Rotate image by a small angle (degrees) around its center.
        Uses white border fill for grayscale, black for color.
        """
        h, w = image.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)

        # Determine border color (white for grayscale receipt paper)
        border_color = 255 if len(image.shape) == 2 else (255, 255, 255)

        rotated = cv2.warpAffine(
            image, M, (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border_color,
        )
        return rotated

    def crop_to_content(self, image: np.ndarray, margin_pct: float = 0.05) -> np.ndarray:
        """Instance method wrapper for crop_to_content_static."""
        return ImagePreprocessor.crop_to_content_static(image, margin_pct)

    @staticmethod
    def crop_to_content_static(image: np.ndarray, margin_pct: float = 0.05) -> np.ndarray:
        """
        Detect the region with ink/content and crop away blank margins.
        Reduces pixel count for faster OCR without losing information.

        Args:
            image: Grayscale image.
            margin_pct: Percentage of margin to keep around content.

        Returns:
            Cropped image (or original if content fills most of frame).
        """
        h, w = image.shape[:2]

        # Don't crop images that are already small
        if w < 400 or h < 400:
            logger.debug(f"  ROI crop skipped (image already small: {w}x{h})")
            return image

        # Threshold to find dark content (ink)
        _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Find bounding rect of all content
        coords = cv2.findNonZero(binary)
        if coords is None:
            return image

        x, y, rw, rh = cv2.boundingRect(coords)

        # Only crop if the content region is significantly smaller than the image
        # (i.e., there are meaningful blank margins to remove)
        content_ratio = (rw * rh) / (w * h)
        if content_ratio > 0.50:
            logger.debug(f"  ROI crop skipped (content fills {content_ratio:.0%} of frame)")
            return image

        # Add generous margin (at least 30px)
        margin_x = max(int(w * margin_pct), 30)
        margin_y = max(int(h * margin_pct), 30)
        x1 = max(0, x - margin_x)
        y1 = max(0, y - margin_y)
        x2 = min(w, x + rw + margin_x)
        y2 = min(h, y + rh + margin_y)

        # Enforce minimum cropped dimensions for OCR to work
        crop_w = x2 - x1
        crop_h = y2 - y1
        if crop_w < 300 or crop_h < 300:
            logger.debug(f"  ROI crop skipped (result too small: {crop_w}x{crop_h})")
            return image

        cropped = image[y1:y2, x1:x2]
        logger.debug(
            f"  ROI crop: {w}x{h} → {crop_w}x{crop_h} "
            f"(content={content_ratio:.0%}, saved {(1-content_ratio)*100:.0f}% pixels)"
        )
        return cropped

    def save_processed_image(
        self, image: np.ndarray, output_path: str
    ) -> str:
        """
        Save processed image to disk.

        Args:
            image: Processed image array.
            output_path: Path to save the image.

        Returns:
            The output path string.
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(output_path, image)
        logger.info(f"Processed image saved: {output_path}")
        return output_path
