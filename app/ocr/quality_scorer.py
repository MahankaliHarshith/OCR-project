"""
Receipt Quality Scorer.

Computes a 0–100 quality score and letter grade (A / B / C / D)
based on multiple OCR and parsing quality signals.

Factors and weights:
    OCR Confidence      30 pts   (avg confidence 0.5→0, 1.0→30)
    Items Found         20 pts   (3+ items → 20)
    Total Verification  15 pts   (qty total matches → 15)
    Math Verification   15 pts   (all line math OK → 15)
    Image Quality       10 pts   (sharpness + brightness)
    Catalog Match Rate  10 pts   (% items matched to catalog)
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class QualityScorer:
    """Computes a quality score and grade for scanned receipts."""

    # Grade thresholds
    GRADE_A = 90
    GRADE_B = 75
    GRADE_C = 60

    def score(
        self,
        items: List[Dict],
        metadata: Dict,
        total_verification: Optional[Dict] = None,
        math_verification: Optional[Dict] = None,
    ) -> Dict:
        """Compute quality score for a scanned receipt.

        Args:
            items: Parsed receipt items.
            metadata: Processing metadata (preprocessing info, OCR stats).
            total_verification: Total qty verification result.
            math_verification: Math/price verification result.

        Returns:
            Dict with score (0-100), grade (A-D), and per-factor breakdown.
        """
        breakdown = {}

        # 1. OCR Confidence (30 points)
        avg_conf = metadata.get("ocr_avg_confidence") or 0
        if avg_conf > 0:
            # Scale: 0.5 conf → 0 pts, 1.0 conf → 30 pts
            conf_score = max(0, min(30, (avg_conf - 0.5) * 60))
        else:
            conf_score = 0
        breakdown["ocr_confidence"] = {
            "score": round(conf_score, 1),
            "max": 30,
            "value": round(avg_conf, 4),
        }

        # 2. Items Found (20 points)
        item_count = len(items)
        if item_count >= 3:
            items_score = 20.0
        elif item_count == 2:
            items_score = 15.0
        elif item_count == 1:
            items_score = 10.0
        else:
            items_score = 0.0
        breakdown["items_found"] = {
            "score": items_score,
            "max": 20,
            "value": item_count,
        }

        # 3. Total Verification (15 points)
        total_score = 0.0
        tv_status = "not_found"
        if total_verification:
            tv_status = total_verification.get("verification_status", "not_found")
            if tv_status == "verified":
                total_score = 15.0
            elif tv_status == "mismatch":
                total_score = 5.0  # at least a total was found
        breakdown["total_verification"] = {
            "score": total_score,
            "max": 15,
            "status": tv_status,
        }

        # 4. Math Verification (15 points)
        math_score = 7.0  # neutral default
        if math_verification:
            if math_verification.get("has_prices"):
                if math_verification.get("all_line_math_ok"):
                    math_score = 15.0
                else:
                    math_score = 8.0
        breakdown["math_verification"] = {"score": math_score, "max": 15}

        # 5. Image Quality (10 points)
        preprocessing = metadata.get("preprocessing", {})
        quality_info = preprocessing.get("quality", {}) if preprocessing else {}

        sharpness = quality_info.get("sharpness", 0) if quality_info else 0
        brightness = quality_info.get("brightness", 128) if quality_info else 128

        img_score = 0.0
        if not quality_info:
            img_score = 5.0  # neutral when no quality data
        else:
            if sharpness > 100:
                img_score += 5
            elif sharpness > 50:
                img_score += 3
            elif sharpness > 20:
                img_score += 1

            if 60 <= brightness <= 200:
                img_score += 5
            elif 40 <= brightness <= 220:
                img_score += 3
            else:
                img_score += 1

        breakdown["image_quality"] = {
            "score": img_score,
            "max": 10,
            "sharpness": round(sharpness, 1) if sharpness is not None else None,
            "brightness": round(brightness, 1) if brightness is not None else None,
        }

        # 6. Catalog Match Rate (10 points)
        if items:
            matched = sum(
                1
                for i in items
                if i.get("match_type") not in (None, "unknown", "azure-unmatched")
            )
            match_rate = matched / len(items)
            catalog_score = round(match_rate * 10, 1)
        else:
            match_rate = 0
            catalog_score = 0.0
        breakdown["catalog_match"] = {
            "score": catalog_score,
            "max": 10,
            "match_rate": round(match_rate, 4),
        }

        # Total score
        total = conf_score + items_score + total_score + math_score + img_score + catalog_score
        total = round(min(100, max(0, total)), 1)

        # Grade
        if total >= self.GRADE_A:
            grade = "A"
        elif total >= self.GRADE_B:
            grade = "B"
        elif total >= self.GRADE_C:
            grade = "C"
        else:
            grade = "D"

        return {
            "score": total,
            "grade": grade,
            "breakdown": breakdown,
        }


# Singleton
quality_scorer = QualityScorer()
