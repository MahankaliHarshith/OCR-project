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

    def extract_text(self, image: np.ndarray) -> List[Dict]:
        """
        Extract text from a preprocessed image.

        Args:
            image: Preprocessed image as numpy array (grayscale or BGR).

        Returns:
            List of detected text elements, each containing:
                - bbox: Bounding box coordinates [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                - text: Detected text string
                - confidence: Confidence score (0.0 to 1.0)
                - needs_review: True if confidence < threshold
        """
        start = time.time()
        logger.debug(f"extract_text called | image shape={image.shape}, dtype={image.dtype}")

        try:
            results = self.reader.readtext(
                image,
                detail=1,
                paragraph=False,       # Individual detections (we group in parser)
                min_size=OCR_MIN_SIZE,
                text_threshold=OCR_TEXT_THRESHOLD,
                low_text=OCR_LOW_TEXT,
                link_threshold=OCR_LINK_THRESHOLD,
                canvas_size=OCR_CANVAS_SIZE,
                mag_ratio=OCR_MAG_RATIO,
                batch_size=1,           # Stable on CPU
                contrast_ths=0.2,       # Lower threshold for faint handwriting
                adjust_contrast=0.9,    # Minimal reduction — preserves faded handwriting ink
                slope_ths=0.4,          # Allow more slanted handwriting
                ycenter_ths=0.5,        # Better vertical grouping for messy writing
                height_ths=0.8,         # More flexible height matching
                width_ths=0.7,          # Tighter to keep alphanumeric codes together (TEW1, PEPW10)
                add_margin=0.15,        # Larger margin captures handwriting ascenders/descenders
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
