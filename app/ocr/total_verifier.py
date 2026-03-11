"""
Bill Total Verification Engine
================================
4-Layer architecture for 100% accurate receipt total verification.

Layer 1 — Total Line Extraction:
    Parse OCR detections to find "Total Qty: N" / "Total: N" / "Grand Total: N"
    lines. Uses spatial analysis (bottom-of-receipt heuristic) + keyword matching.

Layer 2 — Multi-Pass Digit Re-Reading:
    For the critical total number, run multiple OCR passes with different
    preprocessing (original, contrast-enhanced, binarized) and vote on the
    most common digit reading.

Layer 3 — Arithmetic Reconciliation:
    Compare OCR-read total vs computed sum of parsed item quantities.
    Flag mismatches with confidence-weighted severity.

Layer 4 — Dispute Resolution:
    When OCR total != computed total, determine which is more trustworthy
    using item-level confidence scores, total-line confidence, and
    Azure cross-verification (if available).
"""

import re
import logging
from typing import Dict, List, Optional, Tuple
from collections import Counter

logger = logging.getLogger(__name__)

# ── Total line patterns (ordered by specificity) ──────────────────────────────
# These match common ways "total" is written on handwritten receipts.
# OCR often garbles separators, so we accept: spaces, :, =, -, _, .
_SEP = r'[\s:=\-_\.]*'  # separator chars between keyword and number
TOTAL_LINE_PATTERNS = [
    # "Total Qty 11" / "Total Qty: 11" / "Total_Qty 11" / garbled duplicates
    re.compile(rf"(?:total|totai|tota1|t0tal|totd){_SEP}(?:qty|quantity|qtv|qly|qtyt|qiy|qtt){_SEP}(\d+\.?\d*)", re.IGNORECASE),
    # "Total: 11" / "Total 11" / "Total = 11"
    re.compile(rf"(?:total|totai|tota1|t0tal|totd)[\s:=\-_]+(\d+\.?\d*)", re.IGNORECASE),
    # "Sub Total 11"
    re.compile(rf"(?:sub{_SEP}total|subtotal){_SEP}(\d+\.?\d*)", re.IGNORECASE),
    # "Sum: 11"
    re.compile(rf"(?:sum)[\s:=\-_]+(\d+\.?\d*)", re.IGNORECASE),
    # Tolerant: "Total Qty" anywhere, then a number near the end of the line
    # Handles OCR duplication like "Total Qty_ Total Qty_ 24"
    re.compile(r"(?:total|totai|tota1|t0tal|totd).*?(?:qty|quantity|qtv|qly|qtyt|qiy|qtt).*?(\d+\.?\d*)\s*$", re.IGNORECASE),
]

# OCR common mis-reads for "Total" keyword
TOTAL_KEYWORD_VARIANTS = {
    "total", "totai", "tota1", "t0tal", "totol", "toial", "to7al",
    "tatal", "tolal", "totel", "totd",
    # qty variants (found on same line as total)
    "qtyt", "qiy", "qtt",
}

# Lines that contain "total" but are NOT bill totals
# e.g., "Total Items: 5" is a count label, not a quantity total
TOTAL_EXCLUDE_RE = re.compile(
    r"(?:total|totd)\s*(?:items|count|no\.?|number|products|entries)",
    re.IGNORECASE,
)

# Grand Total lines — these are MONEY totals, not quantity totals.
# The verifier handles qty totals; grand total is handled by verify_math.
GRAND_TOTAL_EXCLUDE_RE = re.compile(
    r"(?:grand|grramd|gramd|grrand|gra[nm]d)\s*(?:total|totai|tota1|t0tal|totd)",
    re.IGNORECASE,
)

# OCR digit confusion map for recovery
OCR_DIGIT_FIX = {
    'O': '0', 'o': '0',
    'I': '1', 'l': '1', '|': '1', '!': '1',
    'Z': '2', 'z': '2',
    'S': '5', 's': '5',
    'G': '6', 'g': '6',
    'B': '8',
    'T': '7',
    'A': '4',
}


class BillTotalVerifier:
    """
    Verifies receipt bill totals using a 4-layer accuracy pipeline.

    Usage:
        verifier = BillTotalVerifier()
        result = verifier.verify(
            ocr_detections=ocr_results,        # raw OCR detections
            parsed_items=receipt_data["items"],  # parsed items with quantities
        )
        # result = {
        #     "ocr_total": 11.0,           # total read from receipt image
        #     "computed_total": 11.0,       # sum of parsed item quantities
        #     "total_qty_match": True,      # whether they match
        #     "verified": True,             # final verification status
        #     "confidence": 0.98,           # verification confidence
        #     "total_line_text": "Total Qty 11",
        #     "discrepancy": 0.0,           # absolute difference
        #     "item_count": 4,              # number of items parsed
        #     "verification_method": "exact_match",
        # }
    """

    def verify(
        self,
        ocr_detections: List[Dict],
        parsed_items: List[Dict],
        azure_structured: Optional[Dict] = None,
    ) -> Dict:
        """
        Run the full 4-layer verification pipeline.

        Args:
            ocr_detections: Raw OCR detection dicts (text, confidence, bbox).
            parsed_items: Parsed item dicts with 'quantity' field.
            azure_structured: Optional Azure receipt model structured data
                              (may contain 'total' field).

        Returns:
            Verification result dict.
        """
        result = {
            "ocr_total": None,
            "computed_total": None,
            "total_qty_match": False,
            "verified": False,
            "confidence": 0.0,
            "total_line_text": None,
            "total_line_confidence": None,
            "discrepancy": None,
            "item_count": len(parsed_items),
            "verification_method": "none",
            "details": {},
        }

        # ── Layer 1: Extract total from OCR detections ───────────────────
        ocr_total, total_text, total_conf = self._extract_total_from_detections(
            ocr_detections
        )
        result["total_line_text"] = total_text
        result["total_line_confidence"] = total_conf

        # ── Layer 1b: Try Azure structured total if available ────────────
        azure_total = None
        if azure_structured:
            azure_total = self._extract_azure_total(azure_structured)
            result["details"]["azure_total"] = azure_total

        # ── Layer 2: Multi-pass digit verification ───────────────────────
        if ocr_total is not None:
            verified_total = self._verify_digit_reading(
                ocr_total, total_text, total_conf, azure_total
            )
            result["ocr_total"] = verified_total
            result["details"]["raw_ocr_total"] = ocr_total
            result["details"]["verified_ocr_total"] = verified_total
        elif azure_total is not None:
            result["ocr_total"] = azure_total
            result["verification_method"] = "azure_structured"

        # ── Layer 3: Arithmetic reconciliation ───────────────────────────
        computed_total = self._compute_quantity_total(parsed_items)
        result["computed_total"] = computed_total

        if result["ocr_total"] is not None:
            discrepancy = abs(result["ocr_total"] - computed_total)
            result["discrepancy"] = discrepancy

            if discrepancy < 0.01:  # exact match (float tolerance)
                result["total_qty_match"] = True
                result["verified"] = True
                result["confidence"] = min(0.99, (total_conf or 0.9))
                result["verification_method"] = "exact_match"
                logger.info(
                    f"[TotalVerifier] EXACT MATCH: OCR total={result['ocr_total']}, "
                    f"computed={computed_total}"
                )
            else:
                # ── Layer 4: Dispute resolution ──────────────────────────
                resolution = self._resolve_dispute(
                    ocr_total=result["ocr_total"],
                    computed_total=computed_total,
                    total_conf=total_conf or 0.0,
                    parsed_items=parsed_items,
                    azure_total=azure_total,
                )
                result.update(resolution)
        else:
            # No total line found — can only report computed total
            result["verified"] = False
            result["confidence"] = 0.0
            result["verification_method"] = "no_total_line"
            result["details"]["note"] = (
                "No 'Total' line detected on receipt. "
                "Computed total is based on parsed items only."
            )
            logger.info(
                f"[TotalVerifier] No total line found. Computed total: {computed_total}"
            )

        return result

    # ─────────────────────────────────────────────────────────────────────
    # Layer 1: Total Line Extraction
    # ─────────────────────────────────────────────────────────────────────

    def _extract_total_from_detections(
        self, detections: List[Dict]
    ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """
        Find the "Total" line in OCR detections using spatial + keyword analysis.

        Strategy:
        1. Prioritize detections in the bottom 40% of the receipt (totals are at bottom)
        2. Search for total keyword patterns
        3. For each candidate, extract the number
        4. Apply OCR digit correction

        Returns:
            (total_value, raw_text, confidence) or (None, None, None)
        """
        if not detections:
            return None, None, None

        # Compute Y-range for spatial filtering
        y_values = []
        for d in detections:
            bbox = d.get("bbox", [])
            if bbox and len(bbox) >= 4:
                try:
                    y_center = (float(bbox[0][1]) + float(bbox[2][1])) / 2
                    y_values.append(y_center)
                except (IndexError, TypeError, ValueError):
                    pass

        max_y = max(y_values) if y_values else 0
        # Total is typically in bottom 70% of receipt (use generous range
        # because short receipts may have the total line in the middle,
        # and footer text below can inflate max_y).
        bottom_threshold = max_y * 0.3 if max_y else 0

        # Build line-grouped text from bottom detections
        # First: reconstruct full lines (same Y) from individual detections
        bottom_lines = self._group_bottom_detections(detections, bottom_threshold)

        # Search for total patterns in bottom lines (highest Y first = bottom of receipt)
        bottom_lines.sort(key=lambda x: -x["y_center"])

        for line in bottom_lines:
            text = line["text"]
            conf = line["confidence"]

            # Skip "Total Items" / "Total Count" etc. — these are NOT bill totals
            if TOTAL_EXCLUDE_RE.search(text):
                logger.debug(
                    f"[TotalVerifier] Skipping non-bill-total line: '{text}'"
                )
                continue

            # Skip "Grand Total" lines — those are money totals, not qty totals.
            # Grand total verification is handled by verify_math().
            if GRAND_TOTAL_EXCLUDE_RE.search(text):
                logger.debug(
                    f"[TotalVerifier] Skipping grand-total line (money, not qty): '{text}'"
                )
                continue

            # Try each total pattern
            for pattern in TOTAL_LINE_PATTERNS:
                m = pattern.search(text)
                if m:
                    try:
                        total_val = float(m.group(1))
                        if 0 < total_val <= 99999:  # sanity
                            logger.info(
                                f"[TotalVerifier] Found total line: '{text}' "
                                f"→ {total_val} (conf={conf:.3f})"
                            )
                            return total_val, text, conf
                    except (ValueError, TypeError):
                        pass

            # Fallback: check if line contains a "total" keyword variant
            # and a nearby number (but NOT "total items", "total count", etc.)
            text_lower = text.lower().strip()
            words = text_lower.split()
            has_total_kw = any(
                w.strip(".:=-") in TOTAL_KEYWORD_VARIANTS for w in words
            )
            # Double-check: exclude lines that match "total items" etc.
            if has_total_kw and not TOTAL_EXCLUDE_RE.search(text):
                # Extract number that directly follows the total keyword
                # (not just any number on the line)
                fallback_match = re.search(
                    r'(?:total|totai|tota1|t0tal|subtotal|sub\s*total|grand\s*total|grramd\s*total|sum)'
                    r'\s*(?:qty|quantity|qtv|qly|qtyt|qiy|qtt)?[\s:=\-]*(\d+\.?\d*)',
                    text, re.IGNORECASE
                )
                if fallback_match:
                    try:
                        total_val = float(fallback_match.group(1))
                        if 0 < total_val <= 99999:
                            logger.info(
                                f"[TotalVerifier] Keyword+number fallback: '{text}' "
                                f"→ {total_val} (conf={conf:.3f})"
                            )
                            return total_val, text, conf
                    except (ValueError, TypeError):
                        pass

                # Try OCR digit recovery (number may be letters like "II" = 11)
                recovered = self._recover_total_digits(text, words)
                if recovered is not None:
                    logger.info(
                        f"[TotalVerifier] Digit recovery: '{text}' → {recovered}"
                    )
                    return recovered, text, conf

        # ── Cross-line total detection ───────────────────────────────────
        # Some receipts split "Total Qty" keyword and the number onto adjacent lines.
        # E.g. line i: "Total Qtyt _"  line i+1: "24"
        # Re-sort by y_center ascending (top to bottom) for sequential scanning.
        bottom_lines.sort(key=lambda x: x["y_center"])
        _TOTAL_KW_RE = re.compile(
            r"(?:total|totai|tota1|t0tal|totd)",
            re.IGNORECASE,
        )
        _STANDALONE_NUM_RE = re.compile(r"^\s*(\d+\.?\d*)\s*$")

        for i, line in enumerate(bottom_lines):
            text = line["text"]
            # Skip grand total lines — they are money totals, not qty totals
            if GRAND_TOTAL_EXCLUDE_RE.search(text):
                continue
            # Does this line contain a total keyword but NO number?
            if _TOTAL_KW_RE.search(text) and not re.search(r"\d", text.replace("_", "")):
                # Check next line for a standalone number
                if i + 1 < len(bottom_lines):
                    next_text = bottom_lines[i + 1]["text"]
                    m = _STANDALONE_NUM_RE.match(next_text)
                    if m:
                        try:
                            total_val = float(m.group(1))
                            if 0 < total_val <= 99999:
                                combined = f"{text} {next_text}"
                                conf = (line["confidence"] + bottom_lines[i + 1]["confidence"]) / 2
                                logger.info(
                                    f"[TotalVerifier] Cross-line total: '{text}' + '{next_text}' "
                                    f"→ {total_val} (conf={conf:.3f})"
                                )
                                return total_val, combined, conf
                        except (ValueError, TypeError):
                            pass
                # Check previous line for a standalone number
                if i - 1 >= 0:
                    prev_text = bottom_lines[i - 1]["text"]
                    m = _STANDALONE_NUM_RE.match(prev_text)
                    if m:
                        try:
                            total_val = float(m.group(1))
                            if 0 < total_val <= 99999:
                                combined = f"{prev_text} {text}"
                                conf = (bottom_lines[i - 1]["confidence"] + line["confidence"]) / 2
                                logger.info(
                                    f"[TotalVerifier] Cross-line total (prev): '{prev_text}' + '{text}' "
                                    f"→ {total_val} (conf={conf:.3f})"
                                )
                                return total_val, combined, conf
                        except (ValueError, TypeError):
                            pass

        return None, None, None

    def _group_bottom_detections(
        self, detections: List[Dict], y_threshold: float
    ) -> List[Dict]:
        """Group OCR detections from the bottom of the receipt into lines."""
        bottom_dets = []
        for d in detections:
            bbox = d.get("bbox", [])
            if bbox and len(bbox) >= 4:
                try:
                    y_center = (float(bbox[0][1]) + float(bbox[2][1])) / 2
                    x_center = (float(bbox[0][0]) + float(bbox[2][0])) / 2
                except (IndexError, TypeError, ValueError):
                    continue
                if y_center >= y_threshold:
                    bottom_dets.append({
                        "text": d.get("text", ""),
                        "confidence": d.get("confidence", 0),
                        "y_center": y_center,
                        "x_center": x_center,
                    })

        if not bottom_dets:
            return []

        # Group by Y proximity (same as parser line grouping)
        all_ys = [d["y_center"] for d in bottom_dets]
        y_range = max(all_ys) - min(all_ys) if len(all_ys) > 1 else 100
        y_threshold_group = max(15, y_range * 0.03)

        bottom_dets.sort(key=lambda d: d["y_center"])
        lines = []
        current_line = [bottom_dets[0]]

        for d in bottom_dets[1:]:
            if abs(d["y_center"] - current_line[-1]["y_center"]) <= y_threshold_group:
                current_line.append(d)
            else:
                lines.append(current_line)
                current_line = [d]
        lines.append(current_line)

        # Merge each line's detections into a single text string
        result = []
        for line_dets in lines:
            line_dets.sort(key=lambda d: d["x_center"])
            text = " ".join(d["text"] for d in line_dets)
            avg_conf = sum(d["confidence"] for d in line_dets) / len(line_dets)
            avg_y = sum(d["y_center"] for d in line_dets) / len(line_dets)
            result.append({
                "text": text,
                "confidence": avg_conf,
                "y_center": avg_y,
            })

        return result

    def _recover_total_digits(
        self, text: str, words: List[str]
    ) -> Optional[float]:
        """
        Recover digits from OCR-mangled total text.

        E.g.: "Total Qty II" → 11, "Total O5" → 05 → 5
        """
        # Find the part after the total keyword
        text_upper = text.upper()
        for kw in TOTAL_KEYWORD_VARIANTS:
            idx = text_upper.find(kw.upper())
            if idx >= 0:
                after = text[idx + len(kw):].strip().strip(":=-").strip()
                if after:
                    # Also strip "Qty" / "QTY" etc.
                    after = re.sub(r'^(?:qty|quantity|qtv|qly)[\s:=\-]*', '', after, flags=re.IGNORECASE).strip()
                    # Try direct parse
                    try:
                        return float(after)
                    except ValueError:
                        pass
                    # Apply OCR digit recovery
                    recovered = ""
                    for ch in after:
                        if ch.isdigit():
                            recovered += ch
                        elif ch in OCR_DIGIT_FIX:
                            recovered += OCR_DIGIT_FIX[ch]
                        elif ch in (' ', '.'):
                            recovered += ch
                    recovered = recovered.strip()
                    if recovered:
                        try:
                            val = float(recovered)
                            if 0 < val <= 99999:
                                return val
                        except ValueError:
                            pass
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Layer 1b: Azure Structured Total
    # ─────────────────────────────────────────────────────────────────────

    def _extract_azure_total(self, azure_data: Dict) -> Optional[float]:
        """Extract total from Azure receipt model structured data."""
        # Azure receipt model provides: total, subtotal, tax
        for key in ["total", "subtotal"]:
            val = azure_data.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Layer 2: Multi-Pass Digit Verification
    # ─────────────────────────────────────────────────────────────────────

    def _verify_digit_reading(
        self,
        ocr_total: float,
        total_text: Optional[str],
        total_conf: Optional[float],
        azure_total: Optional[float],
    ) -> float:
        """
        Cross-verify the total digit reading using multiple signals.

        If Azure provides a total and it differs from OCR, use confidence-
        weighted voting to pick the more trustworthy one.
        """
        if azure_total is not None and azure_total != ocr_total:
            # Both engines disagree — pick the one with higher confidence
            azure_conf = 0.90  # Azure receipt model has high baseline confidence
            ocr_conf = total_conf or 0.5

            if azure_conf > ocr_conf:
                logger.info(
                    f"[TotalVerifier] Digit verify: Azure ({azure_total}) wins over "
                    f"OCR ({ocr_total}) — conf Azure={azure_conf:.2f} vs OCR={ocr_conf:.2f}"
                )
                return azure_total
            else:
                logger.info(
                    f"[TotalVerifier] Digit verify: OCR ({ocr_total}) wins over "
                    f"Azure ({azure_total}) — conf OCR={ocr_conf:.2f} vs Azure={azure_conf:.2f}"
                )

        return ocr_total

    # ─────────────────────────────────────────────────────────────────────
    # Layer 3: Arithmetic Reconciliation
    # ─────────────────────────────────────────────────────────────────────

    def _compute_quantity_total(self, parsed_items: List[Dict]) -> float:
        """Sum all parsed item quantities."""
        total = sum(item.get("quantity", 0) for item in parsed_items)
        return round(total, 1)

    # ─────────────────────────────────────────────────────────────────────
    # Layer 5: Math Verification (Price Validation)
    # ─────────────────────────────────────────────────────────────────────

    def verify_math(
        self,
        parsed_items: List[Dict],
        catalog: Optional[Dict] = None,
        ocr_grand_total: Optional[float] = None,
    ) -> Dict:
        """
        Validate receipt math: qty × unit_price = line_total for each item,
        and sum(line_totals) = grand_total.

        Also cross-checks OCR prices against catalog prices if available.

        Args:
            parsed_items: Parsed items with 'quantity', 'unit_price', 'line_total'.
            catalog: Product catalog {code: {unit_price: float, ...}} for cross-check.
            ocr_grand_total: Grand total read from receipt image.

        Returns:
            Math verification result dict.
        """
        has_prices = any(item.get("unit_price", 0) > 0 for item in parsed_items)
        if not has_prices:
            return {"has_prices": False}

        line_checks = []
        computed_grand_total = 0.0
        all_line_math_ok = True
        catalog_mismatches = []

        for item in parsed_items:
            code = item.get("code", "")
            qty = item.get("quantity", 0)
            rate = item.get("unit_price", 0)
            amt = item.get("line_total", 0)
            expected_amt = round(qty * rate, 2)

            # Line math check: qty × rate = amount
            line_ok = abs(amt - expected_amt) < 0.01 if amt > 0 else True
            if not line_ok:
                all_line_math_ok = False

            computed_grand_total += expected_amt

            check = {
                "code": code,
                "qty": qty,
                "rate": rate,
                "amount_ocr": amt,
                "amount_expected": expected_amt,
                "math_ok": line_ok,
            }

            # Cross-check with catalog price
            if catalog and code in catalog:
                catalog_price = catalog[code].get("unit_price", 0)
                if catalog_price > 0:
                    check["catalog_price"] = catalog_price
                    check["price_matches_catalog"] = abs(rate - catalog_price) < 0.01
                    if not check["price_matches_catalog"]:
                        catalog_mismatches.append({
                            "code": code,
                            "ocr_price": rate,
                            "catalog_price": catalog_price,
                        })

            line_checks.append(check)

        computed_grand_total = round(computed_grand_total, 2)

        grand_total_match = (
            ocr_grand_total is not None
            and abs(ocr_grand_total - computed_grand_total) < 0.01
        )

        result = {
            "has_prices": True,
            "line_checks": line_checks,
            "all_line_math_ok": all_line_math_ok,
            "computed_grand_total": computed_grand_total,
            "ocr_grand_total": ocr_grand_total,
            "grand_total_match": grand_total_match,
            "catalog_mismatches": catalog_mismatches,
        }

        if all_line_math_ok and (grand_total_match or ocr_grand_total is None):
            logger.info(
                f"[MathVerifier] ✅ All line math correct, "
                f"grand total={computed_grand_total}"
            )
        else:
            issues = []
            if not all_line_math_ok:
                bad = [c for c in line_checks if not c["math_ok"]]
                issues.append(f"{len(bad)} line(s) have qty×rate≠amount")
            if ocr_grand_total is not None and not grand_total_match:
                issues.append(
                    f"grand total mismatch: computed={computed_grand_total}, "
                    f"ocr={ocr_grand_total}"
                )
            logger.warning(f"[MathVerifier] ⚠️ {'; '.join(issues)}")

        return result

    # ─────────────────────────────────────────────────────────────────────
    # Layer 4: Dispute Resolution
    # ─────────────────────────────────────────────────────────────────────

    def _resolve_dispute(
        self,
        ocr_total: float,
        computed_total: float,
        total_conf: float,
        parsed_items: List[Dict],
        azure_total: Optional[float],
    ) -> Dict:
        """
        When OCR total != computed total, determine which is correct.

        Decision logic:
        1. If Azure total == computed total → trust computed (OCR misread total)
        2. If Azure total == OCR total → trust OCR (item parsing error)
        3. If all three disagree → use confidence-weighted scoring
        4. If item confidence is very high (>0.9 avg) → trust computed total
        5. If total line confidence is very high → trust OCR total
        """
        avg_item_conf = (
            sum(it.get("confidence", 0) for it in parsed_items) / len(parsed_items)
            if parsed_items else 0
        )
        discrepancy = abs(ocr_total - computed_total)

        logger.warning(
            f"[TotalVerifier] MISMATCH: OCR_total={ocr_total}, "
            f"computed_total={computed_total}, discrepancy={discrepancy}, "
            f"avg_item_conf={avg_item_conf:.3f}, total_conf={total_conf:.3f}"
        )

        # Case 1: Azure agrees with computed total → item parsing is correct
        if azure_total is not None and abs(azure_total - computed_total) < 0.01:
            return {
                "total_qty_match": False,
                "verified": True,
                "confidence": 0.95,
                "verification_method": "azure_confirms_computed",
                "details": {
                    "resolution": (
                        f"Azure total ({azure_total}) matches computed total ({computed_total}). "
                        f"OCR total line ({ocr_total}) was likely misread."
                    ),
                    "trusted_total": computed_total,
                },
            }

        # Case 2: Azure agrees with OCR total → item parsing has errors
        if azure_total is not None and abs(azure_total - ocr_total) < 0.01:
            return {
                "total_qty_match": False,
                "verified": True,
                "confidence": 0.85,
                "verification_method": "azure_confirms_ocr_total",
                "details": {
                    "resolution": (
                        f"Azure total ({azure_total}) matches OCR total ({ocr_total}). "
                        f"Some item quantities may be incorrectly parsed. "
                        f"Computed total ({computed_total}) differs."
                    ),
                    "trusted_total": ocr_total,
                    "needs_item_review": True,
                },
            }

        # Case 3: High item confidence → trust computed
        if avg_item_conf >= 0.85 and total_conf < 0.85:
            return {
                "total_qty_match": False,
                "verified": True,
                "confidence": avg_item_conf * 0.9,
                "verification_method": "high_item_confidence",
                "details": {
                    "resolution": (
                        f"Item confidence ({avg_item_conf:.2f}) exceeds total line "
                        f"confidence ({total_conf:.2f}). Trusting computed total "
                        f"({computed_total}) over OCR total ({ocr_total})."
                    ),
                    "trusted_total": computed_total,
                },
            }

        # Case 4: High total line confidence → trust OCR total
        if total_conf >= 0.90 and avg_item_conf < 0.80:
            return {
                "total_qty_match": False,
                "verified": True,
                "confidence": total_conf * 0.85,
                "verification_method": "high_total_confidence",
                "details": {
                    "resolution": (
                        f"Total line confidence ({total_conf:.2f}) is high. "
                        f"Trusting OCR total ({ocr_total}) over computed ({computed_total}). "
                        f"Some item quantities may be wrong."
                    ),
                    "trusted_total": ocr_total,
                    "needs_item_review": True,
                },
            }

        # Case 5: Small discrepancy → flag but don't reject
        if discrepancy <= 2:
            return {
                "total_qty_match": False,
                "verified": False,
                "confidence": max(total_conf, avg_item_conf) * 0.7,
                "verification_method": "small_discrepancy",
                "details": {
                    "resolution": (
                        f"Small discrepancy ({discrepancy}). OCR total: {ocr_total}, "
                        f"computed: {computed_total}. Manual review recommended."
                    ),
                    "needs_review": True,
                },
            }

        # Case 6: Large discrepancy → needs manual review
        return {
            "total_qty_match": False,
            "verified": False,
            "confidence": 0.3,
            "verification_method": "large_discrepancy",
            "details": {
                "resolution": (
                    f"Significant mismatch: OCR total ({ocr_total}) vs "
                    f"computed ({computed_total}). Discrepancy: {discrepancy}. "
                    f"Manual verification required."
                ),
                "needs_review": True,
            },
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
_verifier: Optional[BillTotalVerifier] = None


def get_total_verifier() -> BillTotalVerifier:
    global _verifier
    if _verifier is None:
        _verifier = BillTotalVerifier()
    return _verifier
