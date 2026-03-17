"""
OCR Correction Feedback Loop Service.

Records user corrections to OCR results and builds a lookup table
that improves future parsing accuracy — without any ML.

When a user manually edits an item (changes product code or quantity),
the correction is recorded.  Over time, common OCR misreads are learned:
    "TEWI" → "TEW1"
    "PEPW4O" → "PEPW40"
    "l0" → "10"

The parser checks this correction map BEFORE fuzzy matching,
giving instant fixes for known OCR errors.
"""

import logging
import threading
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CorrectionService:
    """Manages OCR correction feedback loop."""

    def __init__(self):
        self._corrections_cache: Optional[Dict[str, str]] = None
        self._cache_lock = threading.Lock()

    def record_correction(
        self,
        db_instance,
        receipt_id: int,
        item_id: int,
        original_code: str,
        corrected_code: str,
        original_qty: float,
        corrected_qty: float,
        raw_ocr_text: str = "",
    ) -> None:
        """Record a user correction for future learning.

        Only records if there's an actual change (code or qty differ).
        """
        # Only record meaningful corrections
        code_changed = original_code.upper() != corrected_code.upper()
        qty_changed = abs(original_qty - corrected_qty) > 0.01

        if not code_changed and not qty_changed:
            return  # No actual correction

        try:
            db_instance.add_ocr_correction(
                receipt_id=receipt_id,
                item_id=item_id,
                original_code=original_code.upper(),
                corrected_code=corrected_code.upper(),
                original_qty=original_qty,
                corrected_qty=corrected_qty,
                raw_ocr_text=raw_ocr_text,
            )
            logger.info(
                f"OCR correction recorded: '{original_code}' → '{corrected_code}', "
                f"qty {original_qty} → {corrected_qty}"
            )
            # Invalidate cache so next parse uses updated corrections
            with self._cache_lock:
                self._corrections_cache = None
        except Exception as e:
            logger.warning(f"Failed to record OCR correction: {e}")

    def get_corrections_map(self, db_instance) -> Dict[str, str]:
        """Get the corrections lookup map (original_code → corrected_code).

        Builds from historical corrections where the same correction
        has been made at least 2 times (to avoid noise from one-off typos).

        Returns:
            Dict mapping misread codes to correct codes.
        """
        with self._cache_lock:
            if self._corrections_cache is not None:
                return self._corrections_cache

        try:
            corrections = db_instance.get_ocr_corrections_map(min_count=2)
            with self._cache_lock:
                self._corrections_cache = corrections
            return corrections
        except Exception as e:
            logger.debug(f"Corrections map unavailable: {e}")
            return {}

    def apply_correction(
        self, code: str, corrections_map: Dict[str, str]
    ) -> Tuple[str, bool]:
        """Apply a known correction to a product code.

        Args:
            code: The OCR-read product code.
            corrections_map: The corrections lookup map.

        Returns:
            Tuple of (corrected_code, was_corrected).
        """
        upper = code.upper().strip()
        if upper in corrections_map:
            corrected = corrections_map[upper]
            logger.debug(f"Auto-correction applied: '{upper}' → '{corrected}'")
            return corrected, True
        return upper, False

    def get_correction_stats(self, db_instance) -> Dict:
        """Get statistics about OCR corrections.

        Returns summary of most common corrections and their frequency.
        """
        try:
            return db_instance.get_ocr_correction_stats()
        except Exception as e:
            logger.debug(f"Correction stats unavailable: {e}")
            return {
                "total_corrections": 0,
                "unique_patterns": 0,
                "top_corrections": [],
            }

    def invalidate_cache(self) -> None:
        """Force cache invalidation (e.g. after bulk import)."""
        with self._cache_lock:
            self._corrections_cache = None


# Singleton
correction_service = CorrectionService()
