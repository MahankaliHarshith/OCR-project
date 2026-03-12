"""
OCR Engine Module using EasyOCR.
Handles text extraction from preprocessed receipt images
with confidence scoring and fallback support.
"""

import easyocr
import numpy as np
import logging
import time
from typing import List, Dict, Optional

from app.config import (
    OCR_LANGUAGE,
    OCR_USE_GPU,
    OCR_TEXT_THRESHOLD,
    OCR_LOW_TEXT,
    OCR_LINK_THRESHOLD,
    OCR_CANVAS_SIZE,
    OCR_MAG_RATIO,
    OCR_MIN_SIZE,
    OCR_CONFIDENCE_THRESHOLD,
    MODEL_DIR,
)

logger = logging.getLogger(__name__)


class OCREngine:
    """
    OCR engine using EasyOCR for handwritten text recognition.

    Provides text extraction with confidence scores and supports
    GPU acceleration when available.
    """

    def __init__(
        self,
        language: str = OCR_LANGUAGE,
        use_gpu: bool = OCR_USE_GPU,
    ):
        """
        Initialize the EasyOCR reader.

        Args:
            language: Language code for OCR (default: 'en').
            use_gpu: Whether to use GPU acceleration.
        """
        logger.info(f"Initializing EasyOCR reader (lang={language}, gpu={use_gpu})...")
        start = time.time()

        self.reader = easyocr.Reader(
            [language],
            gpu=use_gpu,
            model_storage_directory=str(MODEL_DIR),
            download_enabled=True,
            detector=True,
            recognizer=True,
            verbose=False,
            quantize=True,  # Reduce memory usage
        )

        elapsed = time.time() - start
        logger.info(f"EasyOCR reader initialized in {elapsed:.1f}s")

        # Warmup: run a realistically-sized dummy image through OCR to trigger
        # PyTorch JIT compilation. Without this, the first real scan pays ~5-8s
        # of JIT overhead on CPU, making structured receipts appear slow.
        # Use 480x640 to match typical preprocessed receipt dimensions
        # after CRAFT's internal canvas_size scaling.
        warmup_start = time.time()
        dummy = np.full((640, 480), 200, dtype=np.uint8)  # light gray, receipt-like
        try:
            self.reader.readtext(dummy, detail=0, canvas_size=640, mag_ratio=1.0)
        except Exception:
            pass  # Warmup errors are harmless
        warmup_ms = int((time.time() - warmup_start) * 1000)
        logger.info(f"OCR warmup completed in {warmup_ms}ms")

    def extract_text(self, image: np.ndarray, quality_info: dict = None) -> List[Dict]:
        """
        Extract text from a preprocessed image.

        Args:
            image: Preprocessed image as numpy array (grayscale or BGR).
            quality_info: Optional dict from preprocessor quality assessment.
                          Used to dynamically tune OCR parameters per-image.

        Returns:
            List of detected text elements, each containing:
                - bbox: Bounding box coordinates [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                - text: Detected text string
                - confidence: Confidence score (0.0 to 1.0)
                - needs_review: True if confidence < threshold
        """
        start = time.time()
        logger.debug(f"extract_text called | image shape={image.shape}, dtype={image.dtype}")

        # ── Dynamic parameter tuning based on image quality ──
        # Adjust OCR sensitivity for blurry, dark, or low-contrast images
        # to maximize text detection on challenging inputs.
        text_threshold = OCR_TEXT_THRESHOLD
        low_text = OCR_LOW_TEXT
        contrast_ths = 0.2
        adjust_contrast = 0.9
        add_margin = 0.15
        canvas_size = OCR_CANVAS_SIZE
        mag_ratio = OCR_MAG_RATIO

        if quality_info:
            if quality_info.get("is_blurry"):
                # Blurry: lower thresholds to catch faint text, increase magnification
                text_threshold = max(0.25, text_threshold - 0.1)
                low_text = max(0.15, low_text - 0.1)
                mag_ratio = min(2.5, mag_ratio + 0.4)
                add_margin = 0.20  # Larger margin for fuzzy character boundaries
                logger.debug("  OCR params: BLURRY mode (lower thresholds, higher mag)")
            if quality_info.get("is_low_contrast"):
                # Low contrast: lower contrast threshold so EasyOCR doesn't skip faint text
                contrast_ths = 0.1
                adjust_contrast = 1.0  # Let EasyOCR boost contrast internally
                logger.debug("  OCR params: LOW CONTRAST mode (lower contrast_ths)")
            if quality_info.get("is_too_dark"):
                # Dark image: increase contrast adjustment
                adjust_contrast = 1.2
                contrast_ths = 0.15
                logger.debug("  OCR params: DARK mode (increased adjust_contrast)")

        try:
            results = self.reader.readtext(
                image,
                detail=1,
                paragraph=False,       # Individual detections (we group in parser)
                min_size=OCR_MIN_SIZE,
                text_threshold=text_threshold,
                low_text=low_text,
                link_threshold=OCR_LINK_THRESHOLD,
                canvas_size=canvas_size,
                mag_ratio=mag_ratio,
                batch_size=1,           # Stable on CPU
                contrast_ths=contrast_ths,
                adjust_contrast=adjust_contrast,
                slope_ths=0.4,          # Allow more slanted handwriting
                ycenter_ths=0.5,        # Better vertical grouping for messy writing
                height_ths=0.8,         # More flexible height matching
                width_ths=0.7,          # Tighter to keep alphanumeric codes together (TEW1, PEPW10)
                add_margin=add_margin,  # Captures handwriting ascenders/descenders
            )
        except Exception as e:
            logger.error(f"EasyOCR extraction failed: {e}")
            return []

        elapsed_ms = int((time.time() - start) * 1000)

        detections = []
        for idx, (bbox, text, confidence) in enumerate(results):
            conf_float = round(float(confidence), 4)
            logger.debug(
                f"  Detection [{idx+1}/{len(results)}]: text={text.strip()!r}, "
                f"conf={conf_float:.4f}, bbox_y={bbox[0][1]:.0f}, "
                f"review={'YES' if conf_float < OCR_CONFIDENCE_THRESHOLD else 'no'}"
            )
            # Convert numpy types to native Python types for JSON serialization
            if hasattr(bbox, 'tolist'):
                bbox_native = bbox.tolist()
            else:
                bbox_native = [
                    [float(c) for c in point] if hasattr(point, '__iter__') else float(point)
                    for point in bbox
                ]
            detections.append(
                {
                    "bbox": bbox_native,
                    "text": text.strip(),
                    "confidence": conf_float,
                    "needs_review": bool(conf_float < OCR_CONFIDENCE_THRESHOLD),
                }
            )

        logger.info(
            f"OCR extracted {len(detections)} text elements in {elapsed_ms}ms"
        )

        return detections

    def extract_text_fast(self, image: np.ndarray) -> List[Dict]:
        """
        Fast-path OCR with optimized canvas & mag_ratio for speed.
        Tuned to capture enough detail on the FIRST pass that a second
        pass is rarely needed for same-type receipt scanning.
        """
        start = time.time()
        logger.debug(f"extract_text_fast called | image shape={image.shape}")

        try:
            results = self.reader.readtext(
                image,
                detail=1,
                paragraph=False,
                min_size=OCR_MIN_SIZE,
                text_threshold=OCR_TEXT_THRESHOLD,
                low_text=OCR_LOW_TEXT,
                link_threshold=OCR_LINK_THRESHOLD,
                canvas_size=min(OCR_CANVAS_SIZE, 1024),  # Balanced: enough detail for handwriting
                mag_ratio=1.5,                            # Higher than before for better digit capture
                batch_size=1,
                contrast_ths=0.2,
                adjust_contrast=0.8,      # Better contrast handling for ink on paper
                slope_ths=0.4,
                ycenter_ths=0.5,
                height_ths=0.8,
                width_ths=0.7,            # Tighter grouping keeps codes together (TEW1, PEPW10)
                add_margin=0.12,          # Slightly larger margin for handwriting
            )
        except Exception as e:
            logger.error(f"EasyOCR fast extraction failed: {e}")
            return []

        elapsed_ms = int((time.time() - start) * 1000)
        detections = []
        for idx, (bbox, text, confidence) in enumerate(results):
            conf_float = round(float(confidence), 4)
            if hasattr(bbox, 'tolist'):
                bbox_native = bbox.tolist()
            else:
                bbox_native = [
                    [float(c) for c in point] if hasattr(point, '__iter__') else float(point)
                    for point in bbox
                ]
            detections.append({
                "bbox": bbox_native,
                "text": text.strip(),
                "confidence": conf_float,
                "needs_review": bool(conf_float < OCR_CONFIDENCE_THRESHOLD),
            })

        logger.info(f"OCR fast-pass extracted {len(detections)} elements in {elapsed_ms}ms")
        return detections

    def extract_text_turbo(self, image: np.ndarray) -> List[Dict]:
        """
        Turbo-speed OCR for structured / printed receipts with large clear text.
        Uses aggressive downscaling and minimal processing for 3-5× speedup.
        """
        start = time.time()
        logger.debug(f"extract_text_turbo called | image shape={image.shape}")

        try:
            results = self.reader.readtext(
                image,
                detail=1,
                paragraph=False,
                min_size=OCR_MIN_SIZE,
                text_threshold=0.5,       # Higher — clear printed text
                low_text=0.4,
                link_threshold=0.4,
                canvas_size=640,           # Much smaller canvas
                mag_ratio=1.0,             # No magnification needed
                batch_size=1,
                contrast_ths=0.3,
                adjust_contrast=0.5,
                slope_ths=0.3,
                ycenter_ths=0.5,
                height_ths=0.8,
                width_ths=0.5,
                add_margin=0.1,
            )
        except Exception as e:
            logger.error(f"EasyOCR turbo extraction failed: {e}")
            return []

        elapsed_ms = int((time.time() - start) * 1000)
        detections = []
        for idx, (bbox, text, confidence) in enumerate(results):
            conf_float = round(float(confidence), 4)
            if hasattr(bbox, 'tolist'):
                bbox_native = bbox.tolist()
            else:
                bbox_native = [
                    [float(c) for c in point] if hasattr(point, '__iter__') else float(point)
                    for point in bbox
                ]
            detections.append({
                "bbox": bbox_native,
                "text": text.strip(),
                "confidence": conf_float,
                "needs_review": bool(conf_float < OCR_CONFIDENCE_THRESHOLD),
            })

        logger.info(f"OCR turbo extracted {len(detections)} elements in {elapsed_ms}ms")
        return detections

    @staticmethod
    def calibrate_confidence(text: str, raw_confidence: float) -> float:
        """
        Calibrate raw EasyOCR confidence to produce more realistic scores.

        EasyOCR's CRNN model frequently reports inflated confidence (0.70-0.95)
        on garbled or misread handwritten text. This calibration applies
        evidence-based penalties to surface genuinely uncertain detections.

        Penalties applied:
            - Very short text (1-2 chars): EasyOCR overestimates on fragments
            - High ratio of OCR-confusion characters (digits/symbols in alpha context)
            - Non-alphanumeric noise characters
            - All-digit text that is very long (likely misread)
            - Repetitive character patterns (e.g., "IIII", "0000")

        Args:
            text: The detected text string.
            raw_confidence: EasyOCR's raw confidence score (0.0-1.0).

        Returns:
            Calibrated confidence score (0.0-1.0), always <= raw_confidence.
        """
        if not text or not text.strip():
            return 0.0

        cal = raw_confidence
        stripped = text.strip()
        length = len(stripped)

        # ── Penalty 1: Very short text (1-2 chars) ──
        # EasyOCR often gives 0.8+ confidence on single-char noise fragments
        if length == 1:
            cal *= 0.60  # Heavy penalty — single chars are unreliable
        elif length == 2:
            cal *= 0.80  # Moderate penalty

        # ── Penalty 2: OCR-confusion character ratio ──
        # Characters commonly confused in handwriting OCR
        confusion_chars = set('|![]{}()_=;$&#@~`')
        n_confusion = sum(1 for c in stripped if c in confusion_chars)
        if length > 0:
            confusion_ratio = n_confusion / length
            if confusion_ratio > 0.3:
                cal *= 0.65  # >30% noise chars → very unreliable
            elif confusion_ratio > 0.15:
                cal *= 0.80

        # ── Penalty 3: Repetitive characters ──
        # "IIII", "0000", "llll" — EasyOCR artifacts from ink smudges
        if length >= 3:
            unique_ratio = len(set(stripped.lower())) / length
            if unique_ratio < 0.35:
                cal *= 0.70  # Very repetitive → likely noise

        # ── Penalty 4: All-digit strings > 5 chars ──
        # Long digit strings on handwritten receipts are often misreads
        # (e.g., price read as phone number, date as random digits)
        alpha_count = sum(1 for c in stripped if c.isalpha())
        digit_count = sum(1 for c in stripped if c.isdigit())
        if digit_count > 5 and alpha_count == 0:
            cal *= 0.75

        # ── Penalty 5: Mixed case with many symbols ──
        # "J1L{2" type garbage from handwriting misreads
        symbol_count = sum(1 for c in stripped if not c.isalnum() and c != ' ')
        if symbol_count >= 2 and length <= 6:
            cal *= 0.75

        # ── Bonus: Clean alphanumeric text (3-7 chars) ──
        # Product codes like TEW1, PEPW20, GHI are clean and reliable
        if 3 <= length <= 7 and all(c.isalnum() for c in stripped):
            cal = min(cal * 1.05, raw_confidence)  # Slight boost, never above raw

        return round(min(cal, raw_confidence), 4)  # Never exceed raw confidence

    def extract_text_simple(self, image: np.ndarray) -> List[str]:
        """
        Extract text and return only the text strings (no metadata).

        Args:
            image: Preprocessed image array.

        Returns:
            List of detected text strings.
        """
        detections = self.extract_text(image)
        return [d["text"] for d in detections if d["text"]]

    def get_avg_confidence(self, detections: List[Dict]) -> float:
        """Calculate average confidence across all detections."""
        if not detections:
            return 0.0
        confidences = [d["confidence"] for d in detections]
        return round(sum(confidences) / len(confidences), 4)

    def get_calibrated_avg_confidence(self, detections: List[Dict]) -> float:
        """Calculate average CALIBRATED confidence across all detections.

        Uses calibrate_confidence() to adjust each detection's raw score
        before averaging. This gives a more realistic overall confidence
        that the hybrid engine uses for routing decisions.
        """
        if not detections:
            return 0.0
        cal_confs = [
            self.calibrate_confidence(d.get("text", ""), d.get("confidence", 0))
            for d in detections
        ]
        return round(sum(cal_confs) / len(cal_confs), 4)

    def get_low_confidence_items(
        self, detections: List[Dict], threshold: float = OCR_CONFIDENCE_THRESHOLD
    ) -> List[Dict]:
        """Get detections below the confidence threshold."""
        return [d for d in detections if d["confidence"] < threshold]


# ─── Lazy singleton ──────────────────────────────────────────────────────────

_engine: Optional[OCREngine] = None


def get_ocr_engine() -> OCREngine:
    """Get or create the OCR engine singleton (lazy initialization)."""
    global _engine
    if _engine is None:
        _engine = OCREngine()
    return _engine
