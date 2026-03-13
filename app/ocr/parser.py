"""
Receipt Data Parser Module.
Parses raw OCR output into structured item-quantity pairs
using pattern recognition and product code mapping.
"""

import re
import logging
from typing import List, Dict, Tuple, Optional
from difflib import get_close_matches
from datetime import datetime

from app.config import FUZZY_MATCH_CUTOFF, FUZZY_MAX_RESULTS
from app.tracing import get_tracer, optional_span

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


class ReceiptParser:
    """
    Parses OCR text output into structured receipt data.

    Supports multiple item-quantity formats including handwritten receipts:
        ABC 2, 2 ABC, ABC x 2, ABC - 2, ABC: 2, ABC(2),
        1. ABC - 2qt, ABC 2qt, ABC-2qt., N. CODE NUMBERqt
    """

    # ── Pre-cleaning: strip line numbers and quantity suffixes ──
    # Remove leading line numbers: "1. ", "2) ", "3 - ", etc.
    LINE_NUMBER_RE = re.compile(r"^\s*\d{1,2}\s*[.):\-–—]\s*")
    # Remove "qt", "qt.", "qts", "qty" suffix AFTER a number
    QTY_SUFFIX_RE = re.compile(r"(\d+\.?\d*)\s*(?:qt[sy]?\.?|quantity)", re.IGNORECASE)

    # Regex patterns for item-quantity extraction (ordered by priority)
    # Codes: 3-6 pure alpha (ABC, PAINT) or 2-4 alpha + 1-3 alphanumeric (TEW10, PEPW4)
    _CODE = r'[A-Za-z]{3,6}|[A-Za-z]{2,4}[A-Za-z0-9]{1,3}'  # min 3 chars for pure alpha
    PATTERNS = [
        # CODE - NUMBERqt  (handwritten receipt format with dash)
        re.compile(rf"({_CODE})\s*[-–—]\s*(\d+\.?\d*)", re.IGNORECASE),
        # Boxed template: NUM CODE NUM or NUM CODE OCR_NUM
        # e.g., "1 ABc 2", "5 MNO I0", "3 GHI 1" — first NUM is S.No, last is qty
        re.compile(rf"\d{{1,2}}\s+({_CODE})\s+(\d+\.?\d*)", re.IGNORECASE),
        re.compile(rf"\d{{1,2}}\s+({_CODE})\s+([IlOo|!][0-9IlOo|!]{{0,2}})", re.IGNORECASE),
        # ABC 2 or ABC 2.5  /  TEW1O 5
        re.compile(rf"({_CODE})\s+(\d+\.?\d*)", re.IGNORECASE),
        # 2 ABC (quantity first)
        re.compile(rf"(\d+\.?\d*)\s+({_CODE})", re.IGNORECASE),
        # ABC x 2 or ABC × 2
        re.compile(rf"({_CODE})\s*[xX×]\s*(\d+\.?\d*)", re.IGNORECASE),
        # ABC: 2
        re.compile(rf"({_CODE})\s*:\s*(\d+\.?\d*)", re.IGNORECASE),
        # ABC(2)
        re.compile(rf"({_CODE})\s*\(\s*(\d+\.?\d*)\s*\)", re.IGNORECASE),
        # CODE followed by OCR-mangled number: MNO I0, ABC l0, GHI IO
        # (letter-digit mix where leading letter is OCR confusion for a digit)
        re.compile(rf"({_CODE})\s+([IlOo|!][0-9IlOo|!]{{0,2}})", re.IGNORECASE),
        # Standalone product code: 3+ alpha or alpha+digit code (quantity defaults to 1)
        re.compile(r"^([A-Za-z]{2,4}[A-Za-z0-9]{1,3}|[A-Za-z]{3,6})$", re.IGNORECASE),
    ]

    # Lines to skip (headers, footers, noise)
    SKIP_PATTERNS = [
        re.compile(r"^\s*$"),  # Empty lines
        # NOTE: "total" lines are NO LONGER skipped — they are extracted for
        # bill total verification.  The _is_total_line() method handles them.
        re.compile(r"(date|time|invoice|receipt\s*#)", re.IGNORECASE),
        re.compile(r"(thank|signature|sign|cash|change)", re.IGNORECASE),
        re.compile(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$"),  # Date patterns
        re.compile(r"^\d{1,2}:\d{2}"),  # Time patterns
        re.compile(r"^[#\-=_*]{3,}$"),  # Separator lines
        re.compile(r"^(item|product|code|name|quantity|qty|sr\.?\s*no)", re.IGNORECASE),  # Column headers
        # OCR-mangled 'Items' header: Lte, Ltewu, ltems, etc.
        re.compile(r"^[Ll][Tt][Ee][A-Za-z]*$"),
        # Common receipt words that OCR may read and fuzzy-match to catalog codes
        re.compile(r"^(firma|store|shop|mart|bill|paid|amount|rate|price|unit)$", re.IGNORECASE),
        # OCR garbled versions of QTY/TOTAL that look like codes
        re.compile(r"^(qtx|qix|qty|qtv|qly|qry)$", re.IGNORECASE),
        # Boxed template elements
        re.compile(r"\(\s*(block|capitals|number|only)\s*", re.IGNORECASE),  # (BLOCK CAPITALS), (NUMBER ONLY)
        re.compile(r"^\s*s\.?\s*no\b", re.IGNORECASE),  # S.No header
        re.compile(r"^\s*unit\s*$", re.IGNORECASE),  # UNIT header
        re.compile(r"^\s*receipt\s*#?\s*$", re.IGNORECASE),  # RECEIPT title
        re.compile(r"(prepared\s*by|total\s*items)", re.IGNORECASE),  # Footer fields
        # ── Column header words (standalone or combined) ──
        # "AMOUNT", "RATE", "Item Code", "Qty", "Price", "S.No" as standalone words on a line
        re.compile(r"^\s*(amount|rate|price|amt)\s*$", re.IGNORECASE),
        # Multi-word column header lines: "Item Code  Qty  Rate  Amount"
        re.compile(
            r"^\s*(item\s*code|item|code|qty|quantity|rate|amount|price|s\.?\s*no|unit)"
            r"(\s+(item\s*code|item|code|qty|quantity|rate|amount|price|s\.?\s*no|unit)){1,}",
            re.IGNORECASE,
        ),
    ]

    # Patterns that identify a "Total" line (for bill total verification)
    # OCR often garbles separators, so we accept: spaces, :, =, -, _, .
    _SEP = r'[\s:=\-_\.]*'  # separator chars between keyword and number
    _TOTAL_WORD = r'(?:total|totai|tota1|t0tal|totd|toal|tdal|tetal)'
    TOTAL_LINE_PATTERNS = [
        # "Total Qty 11" / "Total Qty: 11" / "Total_Qty 11" / garbled duplicates
        re.compile(rf"{_TOTAL_WORD}{_SEP}(?:qty|quantity|qtv|qly|qtyt|qiy|qtt){_SEP}(\d+\.?\d*)", re.IGNORECASE),
        # "Grand Total 11" / "Grand Total: 11"
        re.compile(rf"(?:grand|grramd|gramd|grrand|gra[nm]d){_SEP}{_TOTAL_WORD}{_SEP}(\d+\.?\d*)", re.IGNORECASE),
        # "Sub Total 11"
        re.compile(rf"(?:sub{_SEP}total|subtotal){_SEP}(\d+\.?\d*)", re.IGNORECASE),
        # "Total: 11" / "Total 11" / "Total = 11"
        re.compile(rf"{_TOTAL_WORD}[\s:=\-_]+(\d+\.?\d*)", re.IGNORECASE),
        # "Sum: 11"
        re.compile(rf"(?:sum)[\s:=\-_]+(\d+\.?\d*)", re.IGNORECASE),
        # Tolerant: "Total Qty" anywhere, then a number near the end of the line
        # Handles OCR duplication like "Total Qty_ Total Qty_ 24"
        re.compile(rf"{_TOTAL_WORD}.*?(?:qty|quantity|qtv|qly|qtyt|qiy|qtt).*?(\d+\.?\d*)\s*$", re.IGNORECASE),
    ]

    # Quick check: does this line contain a "total"-like keyword?
    _TOTAL_KEYWORD_RE = re.compile(
        r"(total|totai|tota1|t0tal|totd|totol|toial|tatal|toal|tdal|tetal|subtotal|sub\s*total|grand\s*total|sum)", re.IGNORECASE
    )

    # ── Price Line Patterns (4-column: CODE QTY RATE AMOUNT) ──
    # Matches lines like: "TEW1  3  250  750" or "TEW1  3  250.00  750.00"
    # Group 1: code, Group 2: qty, Group 3: rate, Group 4: amount
    # Important: alphanumeric codes (TEW1, PEPW4, TEW20) MUST be matched before
    # pure alpha codes, so the trailing digits stay part of the code.
    _PRICE_CODE = r'[A-Za-z]{2,4}[A-Za-z0-9]{1,3}|[A-Za-z]{3,6}'
    PRICE_LINE_PATTERNS = [
        # CODE  QTY  RATE  AMOUNT (all space-separated)
        re.compile(
            rf"({_PRICE_CODE})\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)",
            re.IGNORECASE,
        ),
        # With line number prefix: N. CODE QTY RATE AMOUNT
        re.compile(
            rf"\d{{1,2}}\s+({_PRICE_CODE})\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)",
            re.IGNORECASE,
        ),
        # CODE  QTY  @RATE  AMOUNT or CODE QTY x RATE = AMOUNT
        re.compile(
            rf"({_PRICE_CODE})\s+(\d+\.?\d*)\s*[@x×]\s*(\d+\.?\d*)\s*[=]?\s*(\d+\.?\d*)",
            re.IGNORECASE,
        ),
    ]

    # ── Grand Total (monetary) Patterns ──
    # Distinct from Total Qty — these match the monetary total line.
    _AMOUNT_SEP = r'[\s:=\-_\.]*'
    GRAND_TOTAL_PATTERNS = [
        # "Grand Total 10150" / "Grand Total: 10150" / "Grramd Total 10150"
        re.compile(rf"(?:grand|grramd|gramd|grrand|gra[nm]d){_AMOUNT_SEP}{_TOTAL_WORD}{_AMOUNT_SEP}(\d+\.?\d*)", re.IGNORECASE),
        # "Total Amount 10150" / "Total Amt 10150"
        re.compile(rf"{_TOTAL_WORD}{_AMOUNT_SEP}(?:amount|amt){_AMOUNT_SEP}(\d+\.?\d*)", re.IGNORECASE),
        # "Bill Total 10150"
        re.compile(rf"(?:bill){_AMOUNT_SEP}{_TOTAL_WORD}{_AMOUNT_SEP}(\d+\.?\d*)", re.IGNORECASE),
        # "Net Total 10150"
        re.compile(rf"(?:net){_AMOUNT_SEP}{_TOTAL_WORD}{_AMOUNT_SEP}(\d+\.?\d*)", re.IGNORECASE),
    ]
    # Quick check for grand total keywords (must NOT be just "Total Qty")
    _GRAND_TOTAL_KEYWORD_RE = re.compile(
        r"((?:grand|grramd|gramd|grrand|gra[nm]d)\s*(?:total|totai|tota1|t0tal|totd|toal|tdal|tetal)"
        r"|(?:total|totai|tota1|t0tal|totd|toal|tdal|tetal)\s*(?:amount|amt)"
        r"|bill\s*(?:total|totai|tota1|t0tal|totd|toal|tdal|tetal)"
        r"|net\s*(?:total|totai|tota1|t0tal|totd|toal|tdal|tetal))",
        re.IGNORECASE,
    )

    def __init__(self, product_catalog: Dict[str, str]):
        """
        Initialize parser with a product catalog.

        Args:
            product_catalog: Dict mapping product codes to product names.
                             Example: {"ABC": "1L Exterior Paint"}
        """
        self.product_catalog = {
            k.upper(): v for k, v in product_catalog.items()
        }
        # LRU cache for _map_product_code results (avoids repeated fuzzy search)
        self._code_match_cache: Dict[str, Tuple[str, str, Optional[str]]] = {}
        self._CODE_CACHE_MAX = 128

    def parse(self, ocr_results: List[Dict], is_structured: bool = False) -> Dict:
        """
        Parse OCR results into structured receipt data.

        Groups individual OCR detections into lines by Y-coordinate,
        then parses each reconstructed line.

        Args:
            ocr_results: List of OCR detection dicts with 'text', 'confidence', and 'bbox'.
            is_structured: Whether the receipt is a structured/boxed template (uses tighter line grouping).

        Returns:
            Structured receipt data dictionary.
        """
        # Start tracing span for receipt parsing
        _parse_span = None
        try:
            _parse_span = _tracer.start_span("receipt_parsing", attributes={
                "parse.detections_in": len(ocr_results),
                "parse.is_structured": is_structured,
            })
        except Exception:
            pass

        items = []
        unparsed_lines = []
        total_line_text = None       # captured total line (for bill verification)
        total_qty_ocr = None         # total value read from the receipt
        total_line_confidence = None # confidence of the total line detection
        grand_total_ocr = None       # monetary grand total from receipt
        grand_total_text = None      # raw text of grand total line
        grand_total_confidence = None
        receipt_number = self._generate_receipt_number()

        # ── GROUP detections into lines by Y-coordinate ──
        grouped_lines = self._group_into_lines(ocr_results, is_structured=is_structured)
        logger.debug(f"Parsing {len(ocr_results)} OCR detections → {len(grouped_lines)} grouped lines for receipt {receipt_number}")

        # ── PRE-SCAN: extract total line BEFORE item parsing ──
        # Prioritize specific patterns (Total Qty) over generic ones.
        # Scan ALL lines and pick the best match rather than taking the first.
        best_total_val = None
        best_total_text = None
        best_total_conf = None
        best_pattern_idx = len(self.TOTAL_LINE_PATTERNS)  # worst priority

        for idx, line_info in enumerate(grouped_lines):
            raw_text = line_info["text"]
            confidence = line_info["confidence"]
            if self._is_total_line(raw_text):
                # Skip lines that are grand totals (monetary) — they should NOT
                # be treated as total qty lines. Grand total extraction happens
                # in a separate scan below.
                if self._GRAND_TOTAL_KEYWORD_RE.search(raw_text):
                    logger.debug(f"  TOTAL QTY SCAN: skipping grand total line: {raw_text!r}")
                    continue
                val, txt = self._extract_total_from_line(raw_text)
                if val is not None:
                    # Determine which pattern matched (lower index = higher priority)
                    pat_idx = len(self.TOTAL_LINE_PATTERNS)  # fallback priority
                    for i, pattern in enumerate(self.TOTAL_LINE_PATTERNS):
                        if pattern.search(raw_text):
                            pat_idx = i
                            break

                    if pat_idx < best_pattern_idx:
                        best_total_val = val
                        best_total_text = txt
                        best_total_conf = confidence
                        best_pattern_idx = pat_idx
                        logger.info(f"  TOTAL LINE candidate: {txt!r} → {val} (pattern={pat_idx}, conf={confidence:.4f})")
                else:
                    # ── CROSS-LINE TOTAL DETECTION ──
                    # Keyword found but no number on the same line.
                    # Check NEXT and PREVIOUS lines for a standalone number.
                    neighbor_indices = []
                    if idx + 1 < len(grouped_lines):
                        neighbor_indices.append(idx + 1)
                    if idx - 1 >= 0:
                        neighbor_indices.append(idx - 1)
                    
                    for nb_idx in neighbor_indices:
                        nb_text = grouped_lines[nb_idx]["text"].strip()
                        nb_conf = grouped_lines[nb_idx]["confidence"]
                        # Accept if neighbor line contains a number
                        # (could be standalone number or "code number" where number is at end)
                        num_match = re.search(r'(\d+\.?\d*)\s*$', nb_text)
                        if num_match:
                            cross_val = float(num_match.group(1))
                            if cross_val > 0 and cross_val <= 999:
                                combined_text = raw_text + " " + num_match.group(1)
                                # Try to match the combined text against patterns
                                combined_val, combined_txt = self._extract_total_from_line(combined_text)
                                if combined_val is not None:
                                    pat_idx = len(self.TOTAL_LINE_PATTERNS) - 1
                                    if pat_idx < best_pattern_idx:
                                        best_total_val = combined_val
                                        best_total_text = combined_text
                                        best_total_conf = min(confidence, nb_conf)
                                        best_pattern_idx = pat_idx
                                        logger.info(
                                            f"  CROSS-LINE TOTAL: '{raw_text}' + '{num_match.group(1)}' → {combined_val}"
                                        )
                                        break
                                else:
                                    # Pattern didn't match but keyword is there
                                    total_kw_line = raw_text.lower()
                                    has_total_kw = any(kw in total_kw_line for kw in ['total', 'totai', 'tota1', 't0tal'])
                                    if has_total_kw:
                                        pat_idx = len(self.TOTAL_LINE_PATTERNS) - 1
                                        if pat_idx < best_pattern_idx:
                                            best_total_val = cross_val
                                            best_total_text = raw_text + " " + num_match.group(1)
                                            best_total_conf = min(confidence, nb_conf)
                                            best_pattern_idx = pat_idx
                                            logger.info(
                                                f"  CROSS-LINE TOTAL (direct): '{raw_text}' + '{num_match.group(1)}' → {cross_val}"
                                            )
                                            break

        if best_total_val is not None:
            total_qty_ocr = best_total_val
            total_line_text = best_total_text
            total_line_confidence = best_total_conf
            logger.info(f"  TOTAL LINE selected: {total_line_text!r} → {total_qty_ocr}")

        # ── PRE-SCAN: extract grand total (monetary) BEFORE item parsing ──
        for idx, line_info in enumerate(grouped_lines):
            raw_text = line_info["text"]
            confidence = line_info["confidence"]
            if self._GRAND_TOTAL_KEYWORD_RE.search(raw_text):
                found_on_line = False
                for pattern in self.GRAND_TOTAL_PATTERNS:
                    m = pattern.search(raw_text)
                    if m:
                        try:
                            val = float(m.group(1))
                            if val > 0:
                                grand_total_ocr = val
                                grand_total_text = raw_text
                                grand_total_confidence = confidence
                                found_on_line = True
                                logger.info(f"  GRAND TOTAL found: {raw_text!r} → {val}")
                        except (ValueError, TypeError):
                            pass
                        break
                # Cross-line grand total: keyword on one line, number on next
                if not found_on_line and idx + 1 < len(grouped_lines):
                    next_text = grouped_lines[idx + 1]["text"].strip()
                    next_conf = grouped_lines[idx + 1]["confidence"]
                    num_match = re.match(r'^\s*(\d+\.?\d*)\s*$', next_text)
                    if num_match:
                        val = float(num_match.group(1))
                        if val > 0:
                            grand_total_ocr = val
                            grand_total_text = raw_text + " " + next_text
                            grand_total_confidence = min(confidence, next_conf)
                            logger.info(
                                f"  CROSS-LINE GRAND TOTAL: '{raw_text}' + '{next_text}' → {val}"
                            )

        for line_info in grouped_lines:
            raw_text = line_info["text"]
            confidence = line_info["confidence"]
            line_y = line_info.get("y_center", 0)

            # Skip empty or noise lines
            if self._should_skip(raw_text):
                logger.debug(f"  SKIP: {raw_text!r}")
                continue

            # ── PRE-CLEAN the OCR text ──
            cleaned = self._clean_ocr_text(raw_text)
            logger.debug(f"  CLEAN: {raw_text!r} → {cleaned!r}")

            # ── PIPE SPLITTING ──
            # OCR sometimes groups multiple items separated by | into one line.
            # Split on pipe and process each segment independently.
            if '|' in cleaned:
                pipe_segments = [s.strip() for s in cleaned.split('|') if s.strip()]
                if len(pipe_segments) > 1:
                    logger.debug(f"  PIPE-SPLIT: {cleaned!r} → {pipe_segments}")
                    for seg in pipe_segments:
                        seg_cleaned = self._clean_ocr_text(seg)
                        if self._should_skip(seg_cleaned):
                            continue
                        seg_parsed = self._parse_line(seg_cleaned, confidence)
                        if seg_parsed:
                            seg_parsed["raw_text"] = raw_text
                            seg_parsed["y_center"] = line_y
                            qt_qty = self._extract_qty_from_qt_marker(seg)
                            if qt_qty is not None:
                                seg_parsed["quantity"] = qt_qty
                            elif seg_parsed["quantity"] <= 1.0:
                                seg_parsed["quantity"] = self._recover_stripped_qty(seg, seg_cleaned)
                            items.append(seg_parsed)
                        else:
                            # Try fuzzy extraction on the segment
                            fuzzy = self._try_fuzzy_code_extraction(seg_cleaned, confidence)
                            if fuzzy:
                                fuzzy["y_center"] = line_y
                                items.append(fuzzy)
                            else:
                                unparsed_lines.append({"text": seg, "confidence": confidence, "y_center": line_y})
                    continue  # Skip normal processing

            # ── MULTI-PRODUCT LINE SPLITTING ──
            # If the line contains 2+ catalog product codes, split and parse each separately
            sub_lines = self._split_multi_product_line(cleaned)
            if sub_lines:
                logger.debug(f"  MULTI-SPLIT: {cleaned!r} → {sub_lines}")
                for sub_text in sub_lines:
                    sub_cleaned = self._clean_ocr_text(sub_text)
                    sub_parsed = self._parse_line(sub_cleaned, confidence)
                    if sub_parsed:
                        sub_parsed["raw_text"] = raw_text
                        sub_parsed["y_center"] = line_y
                        qt_qty = self._extract_qty_from_qt_marker(sub_text)
                        if qt_qty is not None:
                            sub_parsed["quantity"] = qt_qty
                        elif sub_parsed["quantity"] <= 1.0:
                            sub_parsed["quantity"] = self._recover_stripped_qty(sub_text, sub_cleaned)
                        # For structured receipts: "NUM CODE" means S.No + code, not qty + code
                        # This MUST run AFTER _recover_stripped_qty to override it.
                        if is_structured:
                            sno = re.match(r'^\d{1,2}\s+([A-Za-z]{2,6})\s*$', sub_cleaned, re.IGNORECASE)
                            if sno and sub_parsed['quantity'] > 1.0:
                                logger.debug(f"  S.No fix (split): {sub_cleaned!r} qty {sub_parsed['quantity']} -> 1.0")
                                sub_parsed['quantity'] = 1.0
                        logger.debug(
                            f"  PARSED (split): {sub_text!r} → code={sub_parsed['code']}, "
                            f"qty={sub_parsed['quantity']}, match={sub_parsed['match_type']}"
                        )
                        items.append(sub_parsed)
                continue  # Skip normal single-product parsing

            # Try to parse item-quantity pair
            parsed = self._parse_line(cleaned, confidence)

            if parsed:
                parsed["raw_text"] = raw_text  # keep original
                parsed["y_center"] = line_y
                # If qty is default 1.0 and raw_text had a stripped leading number,
                # use that number as qty (it may be the real quantity, not a line number)
                # Priority: QT marker qty > pattern-extracted qty > recovered stripped qty
                qt_qty = self._extract_qty_from_qt_marker(raw_text)
                if qt_qty is not None:
                    parsed["quantity"] = qt_qty
                elif parsed["quantity"] <= 1.0:
                    parsed["quantity"] = self._recover_stripped_qty(raw_text, cleaned)
                # For structured receipts: "NUM CODE" (no trailing qty) means
                # the leading number is a row S.No, not a quantity.
                # This MUST run AFTER _recover_stripped_qty to override it.
                if is_structured:
                    sno = re.match(r'^\d{1,2}\s+([A-Za-z]{2,6})\s*$', cleaned, re.IGNORECASE)
                    if sno and parsed['quantity'] > 1.0:
                        logger.debug(f"  S.No fix: {cleaned!r} qty {parsed['quantity']} \u2192 1.0")
                        parsed['quantity'] = 1.0
                logger.debug(
                    f"  PARSED: {raw_text!r} → code={parsed['code']}, qty={parsed['quantity']}, "
                    f"match={parsed['match_type']}, conf={parsed['confidence']:.4f}"
                )
                items.append(parsed)
            else:
                # ── FALLBACK: try aggressive single-code extraction ──
                fallback = self._try_fuzzy_code_extraction(cleaned, confidence)
                if fallback:
                    fallback["raw_text"] = raw_text
                    fallback["y_center"] = line_y
                    # Priority: QT marker qty > recovered stripped qty
                    qt_qty = self._extract_qty_from_qt_marker(raw_text)
                    if qt_qty is not None:
                        fallback["quantity"] = qt_qty
                    elif fallback["quantity"] <= 1.0:
                        fallback["quantity"] = self._recover_stripped_qty(raw_text, cleaned)
                    logger.debug(
                        f"  FUZZY-FALLBACK: {raw_text!r} → code={fallback['code']}, qty={fallback['quantity']}"
                    )
                    items.append(fallback)
                else:
                    logger.debug(f"  UNPARSED: {raw_text!r} (conf={confidence:.4f})")
                    unparsed_lines.append(
                        {"text": raw_text, "confidence": confidence, "y_center": line_y}
                    )

        # ── ORPHAN QUANTITY ASSOCIATION ──
        # Find unparsed lines that contain only a number (orphan quantities)
        # and associate them with the nearest parsed item that has qty=1.0
        if unparsed_lines and items:
            # Compute max_y from all available y_center values for proximity threshold
            all_y_vals = [it.get("y_center", 0) for it in items if it.get("y_center")] + \
                         [ul.get("y_center", 0) for ul in unparsed_lines if ul.get("y_center")]
            max_y = max(all_y_vals) if all_y_vals else 0
            remaining_unparsed = []
            for uline in unparsed_lines:
                utext = self._clean_ocr_text(uline["text"])
                # Check if the unparsed line is just a number (orphan qty)
                orphan_qty = self._extract_quantity_from_text(utext)
                # Also try OCR decode for dash fragments like '- 4'
                if orphan_qty == 1.0 and re.search(r'[-\u2013\u2014]', uline["text"]):
                    decoded = self._decode_qty_from_dash_fragment(utext)
                    if decoded and 1 <= decoded <= 999:
                        orphan_qty = float(decoded)
                is_pure_qty = bool(re.match(r'^[\s\d.\-~_&]+$', utext))
                if orphan_qty != 1.0 and is_pure_qty and 1 <= orphan_qty <= 999:
                    # Find nearest item with qty=1.0, validating proximity
                    uline_y = uline.get("y_center", 0)
                    best_item = None
                    best_dist = float('inf')
                    # Adaptive proximity: use 6% of max_y (was 4%), which better handles
                    # widely-spaced handwritten receipts while still preventing cross-line jumps
                    orphan_proximity = max_y * 0.06 if max_y else 100
                    for item in items:
                        if item["quantity"] == 1.0:
                            item_y = item.get("y_center", 0)
                            dist = abs(uline_y - item_y) if uline_y and item_y else float('inf')
                            if dist < best_dist and dist < orphan_proximity:
                                best_dist = dist
                                best_item = item
                    if best_item:
                        logger.info(
                            f"  Orphan qty association: '{uline['text']}' qty={orphan_qty} → {best_item['code']}"
                        )
                        best_item["quantity"] = orphan_qty
                        best_item["needs_review"] = True
                        continue
                remaining_unparsed.append(uline)
            unparsed_lines = remaining_unparsed

        # ── CROSS-LINE QUANTITY LOOK-AHEAD ──────────────────────────────────────────
        # Items that still carry the default qty=1.0 may have their real quantity
        # on an adjacent line that the orphan pass couldn't match (e.g. the number
        # was separated by a column gap).  Scan remaining unparsed lines for a
        # bare integer/decimal and assign it to the Y-nearest item that still
        # needs a quantity.
        if items and unparsed_lines:
            items_needing_qty = [
                i for i in items
                if i["quantity"] == 1.0 and i.get("match_type") != "unknown"
            ]
            # Max distance guard: qty must be within ~6% of receipt height
            max_y = max((d.get("y_center", 0) for d in ocr_results), default=0)
            cross_line_threshold = max_y * 0.06 if max_y else 120
            remaining_unparsed_2: list = []
            for uline in unparsed_lines:
                claimed = False
                m = re.match(r'^\s*(\d+\.?\d*)\s*$', uline.get("text", "").strip())
                if m:
                    qty_val = float(m.group(1))
                    if 1 < qty_val <= 999:
                        # Find nearest item, but reject if too far away
                        best_item = None
                        best_dist = float('inf')
                        for item in items_needing_qty:
                            dist = abs(item.get("y_center", 0) - uline.get("y_center", 0))
                            if dist < best_dist:
                                best_dist = dist
                                best_item = item
                        if best_item and best_dist < cross_line_threshold:
                            best_item["quantity"] = qty_val
                            best_item["needs_review"] = True
                            items_needing_qty.remove(best_item)
                            claimed = True
                            logger.info(
                                f"  Cross-line qty: '{uline['text']}' → "
                                f"{best_item['code']} (qty={qty_val}, dist={best_dist:.0f})"
                            )
                        elif best_item:
                            logger.debug(
                                f"  Cross-line qty REJECTED (too far): '{uline['text']}' "
                                f"dist={best_dist:.0f} > threshold={cross_line_threshold:.0f}"
                            )
                if not claimed:
                    remaining_unparsed_2.append(uline)
            unparsed_lines = remaining_unparsed_2

        # ── DUPLICATE CODE AMBIGUITY RESOLUTION ─────────────────────────────────
        # When OCR reads "PEPW1" as "PEPW1O" → matched to PEPW10, but the real
        # PEPW10 also exists.  If the same catalog code appears multiple times
        # and a SHORTER code also exists (e.g. PEPW1 vs PEPW10), resolve by
        # reassigning the ambiguous occurrence to the shorter code.
        items = self._resolve_duplicate_ambiguity(items)

        # Handle duplicates: aggregate quantities for same product code
        items = self._aggregate_duplicates(items)

        # ── FILTER OUT UNKNOWN/PHANTOM CODES ─────────────────────────────────────
        # Items with match_type "unknown" are phantom codes (OCR noise that
        # couldn't be matched to any catalog product). Remove them entirely
        # rather than polluting the results.
        unknown_items = [i for i in items if i.get("match_type") == "unknown"]
        if unknown_items:
            for ui in unknown_items:
                logger.warning(
                    f"  Removing phantom code: {ui['code']} (match_type=unknown, "
                    f"raw='{ui.get('raw_text', '')}')"
                )
            items = [i for i in items if i.get("match_type") != "unknown"]

        # ── QUANTITY SANITY CHECK ────────────────────────────────────────────────
        # Fix items whose quantity looks like an OCR artefact (misread price,
        # date, or barcode fragment).  qty==0 is always wrong; qty>500 is almost
        # certainly a misread (e.g. the year "2024" treated as a quantity).
        # For items WITH price data (4-col), trust them more; for 2-col items,
        # actively correct bad quantities.
        for item in items:
            qty = item["quantity"]
            has_price_data = item.get("unit_price", 0) > 0
            if qty == 0:
                logger.warning(
                    f"  Qty sanity FAIL: {item['code']} qty=0 → setting to 1"
                )
                item["quantity"] = 1.0
                item["needs_review"] = True
            elif qty > 100 and not has_price_data:
                # 2-col parse produced insane qty — reset to 1
                # Threshold 100: for same-receipt-type scanning, realistic
                # quantities rarely exceed 100.  Catches OCR artefacts like
                # qty=240 (VWX on Media(5).jpg) that the old 500 threshold missed.
                logger.warning(
                    f"  Qty sanity FAIL: {item['code']} qty={qty} "
                    f"(no price data, likely OCR artefact) → resetting to 1"
                )
                item["quantity"] = 1.0
                item["needs_review"] = True
            elif qty > 100 and has_price_data:
                # 4-col parse — the qty*rate=amount check already passed.
                # For qty 100-999 with valid math, trust but flag.
                # For qty > 999, almost certainly wrong even with price data.
                if qty > 999:
                    logger.warning(
                        f"  Qty sanity FAIL: {item['code']} qty={qty} "
                        f"(too high even with price data) → resetting to 1"
                    )
                    item["quantity"] = 1.0
                    item["needs_review"] = True
                else:
                    logger.warning(
                        f"  Qty sanity WARNING: {item['code']} qty={qty} "
                        f"(has price data) → flagging for review"
                    )
                    item["needs_review"] = True

        # Calculate stats
        avg_confidence = (
            sum(i["confidence"] for i in items) / len(items) if items else 0
        )
        needs_review = any(i["needs_review"] for i in items) or avg_confidence < 0.85

        logger.info(
            f"Parse complete: {len(items)} items, {len(unparsed_lines)} unparsed, "
            f"avg_conf={avg_confidence:.4f}, needs_review={needs_review}"
        )
        if unparsed_lines:
            logger.debug(f"Unparsed lines: {[l['text'] for l in unparsed_lines]}")

        # ── Math Verification (price-level) ──────────────────────────────────────
        has_prices = any(item.get("unit_price", 0) > 0 for item in items)
        if has_prices:
            line_checks = []
            computed_grand = 0.0
            all_line_ok = True
            for item in items:
                rate = item.get("unit_price", 0)
                qty  = item.get("quantity", 0)
                amt  = item.get("line_total", 0)
                expected = round(qty * rate, 2)
                ok = abs(amt - expected) < 0.01 if amt > 0 else True
                if not ok:
                    all_line_ok = False
                computed_grand += expected
                line_checks.append({
                    "code": item.get("code", ""),
                    "qty": qty, "rate": rate,
                    "amount_ocr": amt, "amount_expected": expected,
                    "math_ok": ok,
                })
            computed_grand = round(computed_grand, 2)
            math_verification = {
                "has_prices": True,
                "line_checks": line_checks,
                "all_line_math_ok": all_line_ok,
                "computed_grand_total": computed_grand,
                "ocr_grand_total": grand_total_ocr,
                "grand_total_text": grand_total_text,
                "grand_total_confidence": grand_total_confidence,
                "grand_total_match": (
                    grand_total_ocr is not None
                    and abs(grand_total_ocr - computed_grand) < 0.01
                ),
            }
        else:
            math_verification = {
                "has_prices": False,
                "ocr_grand_total": grand_total_ocr,
                "grand_total_text": grand_total_text,
                "grand_total_confidence": grand_total_confidence,
            }

        # ── Bill Total Verification ──────────────────────────────────────────────
        computed_total = sum(item.get("quantity", 0) for item in items)
        computed_total = round(computed_total, 1)

        total_verification = {
            "total_qty_ocr": total_qty_ocr,
            "total_qty_computed": computed_total,
            "total_line_text": total_line_text,
            "total_line_confidence": total_line_confidence,
            "total_qty_match": (
                total_qty_ocr is not None
                and abs(total_qty_ocr - computed_total) < 0.01
            ),
            "verification_status": "not_found",
        }
        if total_qty_ocr is not None:
            if total_verification["total_qty_match"]:
                total_verification["verification_status"] = "verified"
                logger.info(
                    f"  ✅ TOTAL VERIFIED: OCR={total_qty_ocr}, computed={computed_total}"
                )
            else:
                total_verification["verification_status"] = "mismatch"
                logger.warning(
                    f"  ⚠️ TOTAL MISMATCH: OCR={total_qty_ocr}, computed={computed_total}, "
                    f"diff={abs(total_qty_ocr - computed_total)}"
                )

        # End parsing span with result attributes
        try:
            if _parse_span is not None:
                _parse_span.set_attribute("parse.items_found", len(items))
                _parse_span.set_attribute("parse.unparsed_lines", len(unparsed_lines))
                _parse_span.set_attribute("parse.avg_confidence", round(avg_confidence, 4))
                _parse_span.set_attribute("parse.needs_review", needs_review)
                _parse_span.set_attribute("parse.has_total_verification", total_qty_ocr is not None)
                _parse_span.end()
        except Exception:
            pass

        return {
            "receipt_id": receipt_number,
            "scan_timestamp": datetime.now().isoformat(),
            "items": items,
            "total_items": len(items),
            "avg_confidence": round(avg_confidence, 4),
            "needs_review": needs_review,
            "unparsed_lines": unparsed_lines,
            "processing_status": "success" if items else "no_items_found",
            "total_verification": total_verification,
            "math_verification": math_verification,
        }

    def _group_into_lines(self, ocr_results: List[Dict], is_structured: bool = False) -> List[Dict]:
        """
        Group individual OCR detections into lines by Y-coordinate.

        EasyOCR with paragraph=False returns individual word/fragment detections.
        This method groups fragments on the same horizontal line (similar Y value)
        and concatenates them left-to-right to reconstruct full lines.

        Args:
            ocr_results: List of OCR dicts with 'text', 'confidence', 'bbox'.
            is_structured: Use tighter Y-threshold for structured receipts.

        Returns:
            List of dicts with 'text' (reconstructed line), 'confidence' (avg).
        """
        if not ocr_results:
            return []

        # Extract Y-center and X-center from each detection's bbox
        detections_with_pos = []
        for r in ocr_results:
            text = r.get("text", "").strip()
            if not text:
                continue
            bbox = r.get("bbox", [])
            conf = r.get("confidence", 0)

            if bbox and len(bbox) >= 4:
                # bbox is [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                try:
                    y_center = (float(bbox[0][1]) + float(bbox[2][1])) / 2
                    x_center = (float(bbox[0][0]) + float(bbox[2][0])) / 2
                except (IndexError, TypeError, ValueError):
                    y_center = 0
                    x_center = 0
            else:
                y_center = 0
                x_center = 0

            detections_with_pos.append({
                "text": text,
                "confidence": conf,
                "y_center": y_center,
                "x_center": x_center,
            })

        if not detections_with_pos:
            return ocr_results  # Fallback: return as-is

        # Filter out top-edge noise (y < 2% of image height)
        # Only filter if ALSO low confidence — prevents dropping first legitimate item
        max_y = max(d["y_center"] for d in detections_with_pos)
        top_edge_threshold = max_y * 0.02
        filtered_detections = []
        for d in detections_with_pos:
            if d["y_center"] < top_edge_threshold and d.get("confidence", 1.0) < 0.3:
                logger.debug(f"    FILTER-OUT top-edge noise: {d['text']!r} (y={d['y_center']:.0f}, conf={d.get('confidence', 0):.2f})")
            else:
                filtered_detections.append(d)
        if filtered_detections:
            detections_with_pos = filtered_detections

        # Sort by Y-center
        detections_with_pos.sort(key=lambda d: d["y_center"])

        # Group by Y-center: detections within Y_THRESHOLD pixels are on the same line
        # Structured receipts have tighter row spacing → use smaller threshold
        max_y = max(d["y_center"] for d in detections_with_pos)
        if is_structured:
            y_threshold = min(40, max(20, max_y * 0.02))  # Tight: 2% capped at 40px
        else:
            y_threshold = min(55, max(30, max_y * 0.025))  # Handwritten: 2.5% capped at 55px

        # ── ADAPTIVE THRESHOLD for dense receipts ──
        # If many detections are packed into a small vertical range,
        # reduce the threshold to avoid merging adjacent rows.
        # This is critical for same-type receipt scanning where all receipts
        # have similar line spacing.
        if len(detections_with_pos) >= 6:
            y_values = sorted(d["y_center"] for d in detections_with_pos)
            y_gaps = [y_values[i+1] - y_values[i] for i in range(len(y_values)-1) if y_values[i+1] - y_values[i] > 3]
            if y_gaps:
                median_gap = sorted(y_gaps)[len(y_gaps) // 2]
                # If the median gap between detections is small, this is a dense receipt
                # Cap the threshold at 70% of the median gap to avoid row merging
                # (was 80% — tighter now to prevent PEPW1↔PEPW10 swap on dense 8-item receipts)
                if median_gap < y_threshold * 1.8:
                    dense_threshold = max(12, median_gap * 0.70)
                    if dense_threshold < y_threshold:
                        logger.debug(
                            f"  Dense receipt detected: median_gap={median_gap:.0f}px, "
                            f"reducing y_threshold {y_threshold:.0f} → {dense_threshold:.0f}"
                        )
                        y_threshold = dense_threshold

        # ── Detect columnar layout (codes left, quantities right) ──
        all_x = [d["x_center"] for d in detections_with_pos]
        max_x_det = max(all_x)
        min_x_det = min(all_x)
        x_span = max_x_det - min_x_det
        has_columns = x_span > 150
        x_mid = min_x_det + x_span * 0.45 if has_columns else 0

        def _is_right_digit(det):
            """Check if detection is a right-column standalone quantity digit."""
            return (has_columns
                    and det["text"].strip().isdigit()
                    and len(det["text"].strip()) <= 2
                    and det["x_center"] > x_mid)

        lines = []
        current_line = [detections_with_pos[0]]

        for det in detections_with_pos[1:]:
            # ── ROTATION-RESISTANT GROUPING ──
            # On rotated receipts, right-column quantity digits shift vertically
            # relative to their left-column codes (up to ~20px at 2.5° rotation).
            # To prevent digits from being grouped with the wrong code:
            # 1. Compute avg_y from LEFT-COLUMN items only (codes), not including
            #    right-column digits that may have shifted Y positions.
            # 2. Use a wider Y-threshold for right-column digits.
            left_dets = [d for d in current_line if not _is_right_digit(d)]
            if left_dets:
                ref_y = sum(d["y_center"] for d in left_dets) / len(left_dets)
            else:
                ref_y = sum(d["y_center"] for d in current_line) / len(current_line)

            if _is_right_digit(det):
                # Right-column digit: slightly wider threshold, compare against code Y only
                # Use 1.25x — enough to catch rotation-shifted digits (up to ~34px)
                # but not so wide as to cross into the next row (typically ~42px gap)
                effective_threshold = y_threshold * 1.25
            else:
                effective_threshold = y_threshold

            if abs(det["y_center"] - ref_y) <= effective_threshold:
                current_line.append(det)
            else:
                lines.append(current_line)
                current_line = [det]
        lines.append(current_line)

        # For each line, sort by X-center (left to right) and concatenate
        # Only filter pure noise (non-informational fragments), but KEEP digits
        # because they may be quantities that the orphan-qty logic will associate
        grouped = []
        for line in lines:
            filtered = []
            for d in line:
                txt = d["text"].strip()
                # Skip fragments that carry zero information: '[.', '%.', '(.'
                if re.match(r'^[\[(%]?\.?$', txt) or re.match(r'^[\[(%]\s*\.\s*$', txt):
                    logger.debug(f"    FILTER-OUT noise fragment: {txt!r} (y={d['y_center']:.0f})")
                    continue
                filtered.append(d)

            if not filtered:
                continue

            filtered.sort(key=lambda d: d["x_center"])
            combined_text = " ".join(d["text"] for d in filtered)
            avg_conf = sum(d["confidence"] for d in filtered) / len(filtered)
            avg_y = sum(d["y_center"] for d in filtered) / len(filtered)
            grouped.append({
                "text": combined_text,
                "confidence": avg_conf,
                "y_center": avg_y,
            })
            logger.debug(
                f"  LINE (y~{avg_y:.0f}): "
                f"{[d['text'] for d in filtered]} → {combined_text!r}"
            )

        # ── MERGE PASS: join code-only lines with adjacent number-only lines ──
        # In dense 4-column receipts, OCR may place the code on one "line"
        # and the numbers (qty, rate, amount) on the next, because the
        # Y-grouping threshold is too tight.  Merge them back together.
        _CODE_ONLY_RE = re.compile(
            r'^\s*(?:[A-Za-z]{2,4}[A-Za-z0-9]{1,3}|[A-Za-z]{3,6})\s*$'
        )
        _NUMS_ONLY_RE = re.compile(
            r'^\s*[\d\s.]+\s*$'  # only digits, spaces, dots
        )

        # ── MERGE PASS: join orphan code lines with adjacent number lines ──
        # Strategy: find code-only lines and try to merge with the closest
        # numbers-only line (before or after). Prefer the one closer in Y.
        merged = list(grouped)
        changed = True
        while changed:
            changed = False
            new_merged = []
            consumed = set()
            for idx in range(len(merged)):
                if idx in consumed:
                    continue
                cur = merged[idx]
                if _CODE_ONLY_RE.match(cur["text"]):
                    # Look at previous and next for numbers-only
                    prev_idx = idx - 1 if idx > 0 and (idx - 1) not in consumed else None
                    next_idx = idx + 1 if idx + 1 < len(merged) and (idx + 1) not in consumed else None

                    prev_ok = (prev_idx is not None
                               and _NUMS_ONLY_RE.match(merged[prev_idx]["text"]))
                    next_ok = (next_idx is not None
                               and _NUMS_ONLY_RE.match(merged[next_idx]["text"]))

                    partner_idx = None
                    if prev_ok and next_ok:
                        # Both adjacent lines are numbers-only — pick closest in Y
                        dy_prev = abs(cur["y_center"] - merged[prev_idx]["y_center"])
                        dy_next = abs(cur["y_center"] - merged[next_idx]["y_center"])
                        partner_idx = prev_idx if dy_prev <= dy_next else next_idx
                    elif prev_ok:
                        partner_idx = prev_idx
                    elif next_ok:
                        partner_idx = next_idx

                    if partner_idx is not None:
                        partner = merged[partner_idx]
                        merged_text = cur["text"].strip() + " " + partner["text"].strip()
                        merged_conf = (cur["confidence"] + partner["confidence"]) / 2
                        merged_y = (cur["y_center"] + partner["y_center"]) / 2
                        # If partner was already added to new_merged (prev case),
                        # we need to replace it
                        if partner_idx < idx:
                            # Partner is previous — remove it from new_merged
                            new_merged = [m for j, m in enumerate(new_merged)
                                          if j != len(new_merged) - 1 or merged[partner_idx] is not m]
                            # Re-check: find and remove the partner entry
                            for ri in range(len(new_merged) - 1, -1, -1):
                                if new_merged[ri] is merged[partner_idx]:
                                    new_merged.pop(ri)
                                    break
                        consumed.add(partner_idx)
                        consumed.add(idx)
                        new_merged.append({
                            "text": merged_text,
                            "confidence": merged_conf,
                            "y_center": merged_y,
                        })
                        logger.debug(
                            f"  MERGE code+nums: {cur['text']!r} + {partner['text']!r} → {merged_text!r}"
                        )
                        changed = True
                        continue
                new_merged.append(cur)
            merged = new_merged

        return merged

    def _parse_line(self, text: str, confidence: float) -> Optional[Dict]:
        """
        Attempt to parse a single line into an item-quantity pair.
        Tries 4-column price format first (CODE QTY RATE AMOUNT),
        then falls back to standard 2-column (CODE QTY).

        Args:
            text: Cleaned text from OCR (line numbers and qt suffixes already removed).
            confidence: OCR confidence score.

        Returns:
            Parsed item dict or None if parsing fails.
        """
        # ── Pre-clean: collapse spaces inside split numbers ──
        # OCR sometimes reads "1800" as "180 0" or "3200" as "32 00".
        # Only apply AFTER the code prefix to avoid corrupting codes like "PEPW10 5".
        # Pattern: (2+ digits)(space)(1-2 digits)(space or end) → merge
        _code_pfx_re = re.compile(
            r'^(\s*(?:[A-Za-z]{2,4}[A-Za-z0-9]{1,3}|[A-Za-z]{3,6})\s+)(.*)',
            re.DOTALL,
        )
        pfx_m = _code_pfx_re.match(text)
        if pfx_m:
            prefix = pfx_m.group(1)
            numeric_tail = pfx_m.group(2)
            _SPLIT_NUM_RE = re.compile(r'(\d{2,})\s(\d{1,2})(?=\s|$)')
            cleaned_tail = numeric_tail
            prev = None
            max_iters = 2  # Limit iterations to prevent runaway merging
            iters = 0
            while cleaned_tail != prev and iters < max_iters:
                prev = cleaned_tail
                cleaned_tail = _SPLIT_NUM_RE.sub(r'\1\2', cleaned_tail)
                iters += 1
            if cleaned_tail != numeric_tail:
                # Validate: the collapse should produce a valid 4-col line.
                # If the collapsed numbers don't form a valid qty*rate=amount
                # relationship, revert — the spaces were likely real separators.
                collapsed_nums = re.findall(r'\d+\.?\d*', cleaned_tail)
                original_nums = re.findall(r'\d+\.?\d*', numeric_tail)
                # Only accept collapse if it reduces the number count
                # AND doesn't produce fewer than 2 numbers (need at least qty + something)
                if len(collapsed_nums) >= 2 and len(collapsed_nums) < len(original_nums):
                    text = prefix + cleaned_tail
                    logger.debug(f"    Split-number collapse: {pfx_m.group(0)!r} → {text!r}")
                elif len(collapsed_nums) == len(original_nums):
                    # Collapse didn't actually merge anything useful — keep original
                    logger.debug(f"    Split-number collapse SKIPPED (no change in token count)")
                else:
                    text = prefix + cleaned_tail
                    logger.debug(f"    Split-number collapse: {pfx_m.group(0)!r} → {text!r}")

        # ── Try 4-column price format first: CODE QTY RATE AMOUNT ──
        for pattern in self.PRICE_LINE_PATTERNS:
            match = pattern.search(text)
            if match:
                code_raw = match.group(1).upper()
                try:
                    qty = float(match.group(2))
                    rate = float(match.group(3))
                    amount = float(match.group(4))
                except (ValueError, TypeError):
                    continue

                # Validate the 4-column parse:
                # 1) All values must be positive
                # 2) qty should be reasonable (1-999) — quantities are typically small
                # 3) amount MUST be approximately qty * rate (within 15% tolerance)
                # 4) rate should be > qty (prices are usually larger than quantities)
                expected_amount = qty * rate
                if rate >= 1 and amount >= 1 and qty >= 1 and qty <= 999:
                    amount_close = abs(amount - expected_amount) / max(expected_amount, 1) < 0.15
                    if amount_close:
                        # Valid price line detected
                        product_name, match_type, matched_code = self._map_product_code(code_raw)
                        final_code = matched_code if matched_code else code_raw
                        product_info = self._get_product_info(final_code)
                        qty = max(1.0, min(9999.0, round(qty, 1)))
                        logger.debug(
                            f"    PRICE LINE: {text!r} → code={final_code}, qty={qty}, "
                            f"rate={rate}, amount={amount}"
                        )
                        return {
                            "code": final_code,
                            "product": product_name,
                            "quantity": qty,
                            "unit": product_info.get("unit", "Piece") if product_info else "Piece",
                            "confidence": confidence,
                            "needs_review": confidence < 0.5 or match_type == "fuzzy",
                            "match_type": match_type,
                            "raw_text": text,
                            "unit_price": rate,
                            "line_total": amount,
                        }

        # ── 3-column fallback: CODE RATE AMOUNT (qty missing from OCR) ──
        # When OCR misses the qty digit, we get CODE + two numbers.
        # Infer qty = amount / rate if it gives a clean integer.
        _3COL_RE = re.compile(
            rf"({self._PRICE_CODE})\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s*$",
            re.IGNORECASE,
        )
        m3 = _3COL_RE.search(text)
        if m3:
            code_raw = m3.group(1).upper()
            n1 = float(m3.group(2))
            n2 = float(m3.group(3))
            # Try interpretation: n1=rate, n2=amount → qty = n2/n1
            if n1 >= 10 and n2 >= n1 and n2 > 0:
                inferred_qty = n2 / n1
                if 0.8 <= inferred_qty <= 999 and abs(inferred_qty - round(inferred_qty)) < 0.05:
                    inferred_qty = round(inferred_qty)
                    product_name, match_type, matched_code = self._map_product_code(code_raw)
                    final_code = matched_code if matched_code else code_raw
                    product_info = self._get_product_info(final_code)
                    # Only accept if code maps to a known product (exact or OCR-variant)
                    if match_type in ("exact", "normalized", "ambiguous_oi"):
                        logger.debug(
                            f"    3-COL INFER: {text!r} → code={final_code}, "
                            f"qty={inferred_qty} (inferred), rate={n1}, amount={n2}"
                        )
                        return {
                            "code": final_code,
                            "product": product_name,
                            "quantity": float(inferred_qty),
                            "unit": product_info.get("unit", "Piece") if product_info else "Piece",
                            "confidence": confidence * 0.9,  # Slightly lower confidence for inferred qty
                            "needs_review": True,
                            "match_type": match_type,
                            "raw_text": text,
                            "unit_price": n1,
                            "line_total": n2,
                        }

        # ── Standard 2-column parsing (CODE QTY) ──
        for pattern in self.PATTERNS:
            match = pattern.search(text)
            if match:
                groups = match.groups()

                # Handle standalone code pattern (only 1 group → default qty 1)
                if len(groups) == 1:
                    code = groups[0].upper()
                    quantity = 1.0
                else:
                    code, quantity = self._identify_code_and_quantity(groups)

                if code and quantity is not None:
                    code_upper = code.upper()

                    # Reject codes that look like quantity suffixes (Iqt, qty, etc.)
                    if self.NOISE_CODE_RE.match(code_upper) or self.QTY_SUFFIX_LIKE_RE.match(code):
                        logger.debug(f"    Rejected qty-suffix-like code: {code!r}")
                        continue

                    # ── CODE+QTY REASSEMBLY CHECK ──
                    # If the "code" is pure alpha and the "quantity" could be
                    # part of an alphanumeric catalog code (e.g., PEPW + 20 = PEPW20),
                    # prefer the combined code interpretation over code + qty.
                    if (quantity is not None and quantity == int(quantity)
                            and re.match(r'^[A-Za-z]{2,4}$', code_upper)):
                        combined = code_upper + str(int(quantity))
                        if combined in self.product_catalog:
                            logger.debug(f"    Code+Qty reassembly: '{code_upper}' + '{int(quantity)}' → '{combined}'")
                            code_upper = combined
                            quantity = 1.0  # Reset qty since the "number" was part of the code

                    # Map product code to name
                    product_name, match_type, matched_code = self._map_product_code(code_upper)
                    # Use the MATCHED catalog code, not the raw OCR code
                    final_code = matched_code if matched_code else code_upper

                    # ── CATALOG PRICE GUARD ──
                    # If the detected "quantity" matches a known catalog price
                    # for this product, the parser probably grabbed the rate
                    # or amount instead of the real qty. Reset to 1.
                    if quantity > 50 and final_code in self.product_catalog:
                        product_info_tmp = self._get_product_info(final_code)
                        if product_info_tmp:
                            from app.services.product_service import ProductService
                            try:
                                _ps = ProductService()
                                _prod = _ps.get_product(final_code)
                                if _prod and _prod.get('price'):
                                    cat_price = float(_prod['price'])
                                    # If qty matches the catalog price, it's the rate not qty
                                    if abs(quantity - cat_price) < 1:
                                        logger.warning(
                                            f"    Qty={quantity} matches catalog price for {final_code}. "
                                            f"Likely OCR picked up rate as qty. Resetting qty=1."
                                        )
                                        quantity = 1.0
                                    # If qty is a multiple of the catalog price, it's the line total
                                    elif cat_price > 0 and quantity >= cat_price:
                                        inferred = quantity / cat_price
                                        if abs(inferred - round(inferred)) < 0.05 and 1 <= round(inferred) <= 99:
                                            logger.warning(
                                                f"    Qty={quantity} looks like line total for {final_code} "
                                                f"(price={cat_price}). Inferred qty={round(inferred)}."
                                            )
                                            quantity = float(round(inferred))
                            except Exception:
                                pass

                    # Note: Leading "N. " / "N) " line numbers are already
                    # stripped by LINE_NUMBER_RE in _clean_ocr_text().
                    # If a digit remains before the code (e.g. "4 JkL"), treat
                    # it as the quantity — the pre-cleaning already handled
                    # the "line number + punctuation" format.

                    product_info = self._get_product_info(final_code)

                    # Quantity sanity: clamp to [1, 50] for 2-col parse, round to 1 decimal
                    # For same-type receipt scanning, quantities above 50 in a 2-col
                    # format (no rate/amount columns) are almost always OCR misreads.
                    # With a price line (4-col), larger quantities are validated by
                    # qty*rate=amount math, so they don't reach this code path.
                    if quantity <= 0:
                        quantity = 1.0
                    elif quantity > 50:
                        logger.warning(
                            f"    2-col qty={quantity} too high for {final_code}, clamping to 1"
                        )
                        quantity = 1.0
                    quantity = round(quantity, 1)

                    return {
                        "code": final_code,
                        "product": product_name,
                        "quantity": quantity,
                        "unit": product_info.get("unit", "Piece") if product_info else "Piece",
                        "confidence": confidence,
                        "needs_review": confidence < 0.5 or match_type == "fuzzy",
                        "match_type": match_type,
                        "raw_text": text,
                    }

        return None

    # OCR confusion substitution table (digit/symbol → likely letter)
    OCR_CHAR_SUBS = {
        '|': 'I', '1': 'I', '!': 'I',
        '0': 'O',
        '6': 'G',
        '8': 'B',
        '5': 'S',
        '9': 'G',  # sometimes 9 → g
        '(': 'C',
        ')': 'J',
        '_': '',   # just noise
        '2': 'Z',  # 2 ↔ Z in some handwriting
        '4': 'A',  # 4 ↔ A in sloppy writing
        '7': 'T',  # 7 ↔ T (cross-bar confusion)
        '3': 'E',  # 3 ↔ E (mirror confusion)
    }

    # Reverse OCR confusion table (letter → likely digit)
    # When OCR reads an all-alpha code like "TEWI", the trailing letters
    # might actually be digits in the catalog code (TEW1).
    OCR_REVERSE_SUBS = {
        'I': '1', 'L': '1',  # I/l often OCR'd from 1
        'O': '0',            # O often OCR'd from 0
        'Z': '2',            # Z often OCR'd from 2
        'S': '5',            # S often OCR'd from 5
        'G': '6',            # G often OCR'd from 6
        'B': '8',            # B often OCR'd from 8
        'A': '4',            # A sometimes OCR'd from 4
        'T': '7',            # T sometimes OCR'd from 7
        'E': '3',            # E sometimes OCR'd from 3 (mirror)
        'D': '0',            # D ↔ 0 in rounded handwriting
        'Q': '9',            # Q ↔ 9 (tail confusion)
    }

    # Handwriting letter-to-letter confusion map (lowercase OCR output → likely uppercase)
    # These are applied as ADDITIONAL single-char variant substitutions
    HANDWRITING_SUBS = {
        'n': 'H',   # handwritten n ↔ h
        'l': 'I',   # handwritten l ↔ I (lowercase L looks like uppercase I)
        'q': 'G',   # q ↔ g in sloppy handwriting
        'u': 'V',   # u ↔ v
        'w': 'M',   # w ↔ m when inverted
        'p': 'R',   # p ↔ r in some hands
        'c': 'C',   # c ↔ C (case confusion)
        'k': 'K',   # k ↔ K
        'e': 'C',   # e ↔ c in rushed handwriting
        'a': 'O',   # a ↔ o (open loop)
        'f': 'F',   # f ↔ F
    }

    # Patterns for line-number-like fragments: "2 .", "[.", "%.", standalone single digits
    LINE_NUM_FRAGMENT_RE = re.compile(r'^\s*[\[(%]?\s*\d\s*[.)\]]?\s*$')
    # Pattern to detect "N. " or "N " at the beginning (single digit line number)
    LEADING_LINE_NUM_RE = re.compile(r'^\s*(\d)\s*[.)]?\s+')

    def _recover_stripped_qty(self, raw_text: str, cleaned_text: str) -> float:
        """
        If _clean_ocr_text stripped a leading number (thinking it was a line
        number), recover it and return it as the quantity.

        On numbered-line receipts like "2 . DEF - 3qt", both "2" (line num)
        and "3" (quantity) may be present.  But sometimes the OCR merges
        the quantity into the line-number slot, e.g. "2 . def" where 2 IS
        the quantity.

        Heuristic: if the raw text starts with a number that was stripped,
        AND no other quantity was found in the cleaned text, use it.
        """
        # Find what was stripped
        m = re.match(r'^\s*(\d{1,2})\s*[.):\-–—]?\s*', raw_text)
        if not m:
            return 1.0
        stripped_num = float(m.group(1))
        if stripped_num < 1 or stripped_num > 99:
            return 1.0
        # Check the remainder (text after the leading number)
        remainder = raw_text[m.end():]
        # If remainder has a dash-qty, use that
        dash_qty = re.search(r'[-–—]\s*(\d{1,3})', remainder)
        if dash_qty:
            return float(dash_qty.group(1))
        # If remainder has BOTH a product code AND a trailing number,
        # the leading number is likely a line/serial number, not a quantity.
        # e.g., "3 GHI 1" → 3=S.No, GHI=code, 1=qty → do NOT use 3 as qty
        if re.search(r'[A-Za-z]{2,6}', remainder) and re.search(r'\d+', remainder):
            logger.debug(
                f"    Skip recover: remainder {remainder!r} has both code and qty"
            )
            return 1.0
        logger.debug(f"    Recovered stripped qty: {stripped_num} from raw text {raw_text!r}")
        return stripped_num
    def _extract_qty_from_qt_marker(self, raw_text: str) -> Optional[float]:
        """
        Extract quantity by finding a number adjacent to a QT (quantity) suffix.
        Works on RAW text (before noise char removal) to preserve '&' as 'Q'.

        Receipt format: '<line_num>. <code> <qty>QT'
        OCR variants of QT: QT, Q1, QI, Qt, qt, &T, &1, &, 4T, 41
        OCR-confused digits: I->1, O->0, l->1, i->1, (->1
        """
        if not raw_text:
            return None

        # Digit class: standard OCR + handwriting confusions for digits
        # I/l/|/!/( -> 1, O/o -> 0, '/} -> 3, ` -> 1
        _DC = r"[IOlio(0-9'}`]"
        qt_pats = [
            r'(' + _DC + r'{1,3})\s*[Qq][Tt1I]',           # QT, Q1, QI, qt
            r'(' + _DC + r'{1,3})\s*&[Tt1]?',              # &T, &1, &
            r'(' + _DC + r'{1,3})\s*4[Tt1]',               # 4T, 41
            r'(' + _DC + r"{1,3})\s*[Qq&][\s\"'.)\]]*$",   # standalone Q/& at end
            r'(?<=[\s\-–—])(' + _DC + r'{1,3})\s*[Tt][Tt]?\s*[.\s]*$',  # digit + t/tt at end (requires preceding space/dash)
        ]

        last_qty = None
        last_pos = -1

        for pat in qt_pats:
            for m in re.finditer(pat, raw_text):
                digit_str = m.group(1)
                decoded = ''
                for ch in digit_str:
                    if ch.isdigit():
                        decoded += ch
                    elif ch in ('I', 'i', 'l', '|', '!', '('):
                        decoded += '1'
                    elif ch in ('O', 'o'):
                        decoded += '0'
                    elif ch in ("'", '}'):
                        decoded += '3'
                    elif ch == '`':
                        decoded += '1'
                if decoded:
                    try:
                        val = int(decoded)
                        if 1 <= val <= 999 and m.start() > last_pos:
                            last_qty = val
                            last_pos = m.start()
                    except ValueError:
                        pass

        if last_qty is not None:
            logger.debug(f"    QT marker qty: {last_qty} from {raw_text!r}")
            return float(last_qty)
        return None

    def _clean_ocr_text(self, text: str) -> str:
        """
        Pre-clean raw OCR text for better parsing.

        Removes line numbers, quantity suffixes, and OCR noise characters.
        Also reassembles split alphanumeric catalog codes (PEPW 20 → PEPW20).
        """
        cleaned = text

        # Strip leading line numbers:  "1. ", "2) ", "3 - "
        cleaned = self.LINE_NUMBER_RE.sub("", cleaned)

        # Remove pure noise characters (but NOT digits/pipes that could be letters)
        cleaned = cleaned.replace("{", "").replace("}", "")
        cleaned = cleaned.replace("[", "").replace("]", "")
        cleaned = cleaned.replace("&", "").replace("$", "").replace("#", "")
        cleaned = cleaned.replace("_", "").replace("=", "")
        cleaned = cleaned.replace(";", "")  # semicolons are OCR noise from paper texture

        # Strip "qt", "qt.", "qts" after numbers: "2qt" → "2", "10qt." → "10"
        cleaned = self.QTY_SUFFIX_RE.sub(r"\1", cleaned)

        # Normalize whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        # ── REASSEMBLE SPLIT ALPHANUMERIC CODES ──
        # OCR often splits codes like PEPW20 into "PEPW 20" (alpha + space + digits)
        # or "PEP W1" (alpha + alpha-digit fragment).
        # Glue them back if the combined token is a valid catalog code.
        if hasattr(self, 'product_catalog') and self.product_catalog:
            tokens = cleaned.split()
            reassembled = []
            i = 0
            while i < len(tokens):
                if i + 1 < len(tokens):
                    t1 = tokens[i]
                    t2 = tokens[i + 1]
                    # Case 1: ALPHA + DIGITS → "PEPW" + "20" = "PEPW20"
                    if (re.match(r'^[A-Za-z]{2,4}$', t1)
                            and re.match(r'^\d{1,3}$', t2)):
                        combined = (t1 + t2).upper()
                        if combined in self.product_catalog:
                            reassembled.append(t1 + t2)
                            logger.debug(f"    Reassembled split code: '{t1}' + '{t2}' → '{combined}'")
                            i += 2
                            continue
                    # Case 2: ALPHA + ALPHA-DIGIT → "PEP" + "W1" = "PEPW1"
                    #         or "TEw" + "20" = "TEW20" (lowercase letters)
                    if (re.match(r'^[A-Za-z]{2,4}$', t1)
                            and re.match(r'^[A-Za-z]\d{1,3}$', t2)):
                        combined = (t1 + t2).upper()
                        if combined in self.product_catalog:
                            reassembled.append(t1 + t2)
                            logger.debug(f"    Reassembled split code: '{t1}' + '{t2}' → '{combined}'")
                            i += 2
                            continue
                    # Case 3: ALPHA-DIGIT + DIGIT → "TEw2" + "0" = "TEW20"
                    #         Handles codes split where part of the trailing digits
                    #         became a separate token due to spacing/OCR.
                    if (re.match(r'^[A-Za-z]{2,4}\d{1,2}$', t1)
                            and re.match(r'^\d{1,2}$', t2)):
                        combined = (t1 + t2).upper()
                        if combined in self.product_catalog:
                            reassembled.append(t1 + t2)
                            logger.debug(f"    Reassembled split code: '{t1}' + '{t2}' → '{combined}'")
                            i += 2
                            continue
                        # Also try OCR substitutions on the combined result
                        for v in self._generate_ocr_variants(t1 + t2):
                            if v in self.product_catalog:
                                reassembled.append(v)
                                logger.debug(f"    Reassembled+OCR split code: '{t1}' + '{t2}' → '{v}'")
                                i += 2
                                break
                        else:
                            reassembled.append(tokens[i])
                            i += 1
                        continue
                reassembled.append(tokens[i])
                i += 1
            cleaned = ' '.join(reassembled)

        return cleaned

    def _apply_ocr_substitution(self, token: str) -> str:
        """
        Apply digit-to-letter substitution for common OCR confusion pairs.
        e.g., '6n|' → 'GNI', 'J1L' → 'JIL' → 'JKL'
        """
        result = []
        for ch in token:
            if ch in self.OCR_CHAR_SUBS:
                result.append(self.OCR_CHAR_SUBS[ch])
            else:
                result.append(ch)
        return ''.join(result).upper()

    def _generate_ocr_variants(self, token: str) -> List[str]:
        """
        Generate possible interpretations of a token by substituting
        OCR-confused characters. Returns the original + substituted variants.
        Includes digit/symbol→letter, letter→letter, AND letter→digit (reverse)
        confusions to support alphanumeric product codes like TEW1, PEPW10.
        """
        upper = token.upper()
        variants = [upper]

        # Full digit/symbol substitution
        subbed = self._apply_ocr_substitution(token)
        if subbed != upper and subbed:
            variants.append(subbed)

        # Full handwriting substitution (apply all letter swaps at once)
        hw_result = []
        for ch in token:
            if ch in self.OCR_CHAR_SUBS:
                hw_result.append(self.OCR_CHAR_SUBS[ch])
            elif ch.lower() in self.HANDWRITING_SUBS:
                hw_result.append(self.HANDWRITING_SUBS[ch.lower()])
            else:
                hw_result.append(ch)
        hw_full = ''.join(hw_result).upper()
        if hw_full not in variants and hw_full:
            variants.append(hw_full)

        # Single-character substitutions from OCR_CHAR_SUBS
        for i, ch in enumerate(token):
            if ch in self.OCR_CHAR_SUBS:
                variant = token[:i] + self.OCR_CHAR_SUBS[ch] + token[i+1:]
                v_upper = variant.upper()
                if v_upper not in variants and v_upper:
                    variants.append(v_upper)

        # Single-character substitutions from HANDWRITING_SUBS
        for i, ch in enumerate(token):
            ch_lower = ch.lower()
            if ch_lower in self.HANDWRITING_SUBS:
                variant = token[:i] + self.HANDWRITING_SUBS[ch_lower] + token[i+1:]
                v_upper = variant.upper()
                if v_upper not in variants and v_upper:
                    variants.append(v_upper)

        # ── Reverse OCR substitutions (letter → digit) ──
        # Supports alphanumeric catalog codes like TEW1, TEW10, PEPW20.
        # OCR reads digits as letters (1→I, 0→O, 2→Z), so we try reversing
        # the trailing letter portion back to digits.
        # Full reverse substitution (all eligible trailing letters → digits)
        rev_result = list(upper)
        changed = False
        for i in range(len(rev_result)):
            if rev_result[i] in self.OCR_REVERSE_SUBS:
                rev_result[i] = self.OCR_REVERSE_SUBS[rev_result[i]]
                changed = True
        if changed:
            rev_full = ''.join(rev_result)
            if rev_full not in variants:
                variants.append(rev_full)

        # Single-character reverse substitutions
        for i, ch in enumerate(upper):
            if ch in self.OCR_REVERSE_SUBS:
                variant = upper[:i] + self.OCR_REVERSE_SUBS[ch] + upper[i+1:]
                if variant not in variants:
                    variants.append(variant)

        # Pairwise reverse substitutions (two chars at a time)
        # Handles cases like TEWZO → TEW20 (Z→2 + O→0)
        rev_positions = [(i, ch) for i, ch in enumerate(upper) if ch in self.OCR_REVERSE_SUBS]
        if len(rev_positions) >= 2:
            from itertools import combinations
            for (i1, c1), (i2, c2) in combinations(rev_positions, 2):
                chars = list(upper)
                chars[i1] = self.OCR_REVERSE_SUBS[c1]
                chars[i2] = self.OCR_REVERSE_SUBS[c2]
                variant = ''.join(chars)
                if variant not in variants:
                    variants.append(variant)

        return variants

    # Common header words on receipts (to avoid false-positive matching)
    HEADER_WORDS = {"ITEMS", "ITEM", "QUANTITY", "QTY", "PRODUCT", "CODE",
                    "NAME", "TOTAL", "DATE", "SERIAL", "NUMBER", "PRICE",
                    "AMOUNT", "RATE", "UNIT", "BILL", "FIRMA", "STORE",
                    "SHOP", "MART", "PAID", "GRAND", "SUBTOTAL", "SUM",
                    "QTX", "QTY", "QIX", "QTV", "QLY", "QRY"}

    def _try_fuzzy_code_extraction(self, text: str, confidence: float) -> Optional[Dict]:
        """
        Last-resort: extract tokens (including mixed alpha-digit) from the text,
        apply OCR character substitution, and try fuzzy matching against the catalog.
        """
        # Split by whitespace and delimiters to get clean tokens
        raw_tokens = re.split(r'[\s\-\u2013\u2014.,;:]+', text)
        # Keep tokens of 3-7 chars that have at least one letter
        # (min 3 chars prevents 2-char noise like 'tu', 'Iq' from matching)
        tokens = [t for t in raw_tokens
                  if 3 <= len(t) <= 7 and re.search(r'[A-Za-z]', t)]

        # ── SCATTERED LETTER RECONSTRUCTION ──
        # Try assembling single-char letter/digit tokens into a product code.
        # Handles OCR fragments like '6 H [' → 'GHI', '6 H !' → 'GHI'
        single_chars = [t for t in raw_tokens if len(t) == 1 and (t.isalpha() or t in self.OCR_CHAR_SUBS)]
        if len(single_chars) >= 2:
            # Try combining 2-4 adjacent single chars
            for combo_len in range(min(4, len(single_chars)), 1, -1):
                for start in range(len(single_chars) - combo_len + 1):
                    combo = ''.join(single_chars[start:start + combo_len])
                    # Apply OCR character substitution
                    variants = self._generate_ocr_variants(combo)
                    for variant in variants:
                        if variant in self.product_catalog:
                            qty = self._extract_qty_with_ocr_decode(text, combo)
                            logger.info(f"Scattered-letter match: '{combo}' → '{variant}' ({self.product_catalog[variant]})")
                            return {
                                "code": variant,
                                "product": self.product_catalog[variant],
                                "quantity": qty,
                                "unit": "Piece",
                                "confidence": confidence,
                                "needs_review": True,
                                "match_type": "scattered_letter",
                                "raw_text": text,
                            }
                        # Also try fuzzy match
                        from app.config import get_adaptive_fuzzy_cutoff as _gafc
                        matches = get_close_matches(variant, self.product_catalog.keys(), n=1, cutoff=_gafc(len(variant)))
                        if matches:
                            best = matches[0]
                            if abs(len(variant) - len(best)) <= 1 and (variant[0] == best[0] or variant[-1] == best[-1]):
                                qty = self._extract_qty_with_ocr_decode(text, combo)
                                logger.info(f"Scattered-letter fuzzy: '{combo}' → '{variant}' → '{best}' ({self.product_catalog[best]})")
                                return {
                                    "code": best,
                                    "product": self.product_catalog[best],
                                    "quantity": qty,
                                    "unit": "Piece",
                                    "confidence": confidence,
                                    "needs_review": True,
                                    "match_type": "scattered_letter",
                                    "raw_text": text,
                                }

        for token in tokens:
            # Skip obvious noise words and pure numbers
            upper = token.upper()
            if upper in ("THE", "AND", "FOR", "QTY", "ITEM", "CODE", "NAME"):
                continue
            if token.isdigit():
                continue
            # Skip tokens that look like mangled quantity suffixes: Iqt, Io1f, etc.
            # These start with I/l (→1) or O/o (→0) followed by qt/f/digits
            if re.match(r'^[IlOo][qQtTfF0-9]+$', token):
                logger.debug(f"    Skipping qty-suffix token: {token!r}")
                continue

            # Skip if token looks like a header word (fuzzy check)
            header_match = get_close_matches(upper, self.HEADER_WORDS, n=1, cutoff=0.6)
            if header_match:
                logger.debug(f"    Skipping header-like token: {token!r} → {header_match[0]}")
                continue

            # Generate OCR variants (original + substituted versions)
            variants = self._generate_ocr_variants(token)
            logger.debug(f"    Trying token {token!r} → variants: {variants}")

            for variant in variants:
                if not variant or len(variant) < 3:
                    continue

                # Try exact match
                if variant in self.product_catalog:
                    qty = self._extract_qty_with_ocr_decode(text, token)
                    logger.info(f"OCR variant match: '{token}' → '{variant}' (exact)")
                    return {
                        "code": variant,
                        "product": self.product_catalog[variant],
                        "quantity": qty,
                        "unit": "Piece",
                        "confidence": confidence,
                        "needs_review": True,
                        "match_type": "ocr_variant",
                        "raw_text": text,
                    }

                # Try fuzzy match on the variant (length-adaptive cutoff)
                from app.config import get_adaptive_fuzzy_cutoff
                _adaptive_cutoff = get_adaptive_fuzzy_cutoff(len(variant))
                matches = get_close_matches(
                    variant,
                    self.product_catalog.keys(),
                    n=1,
                    cutoff=_adaptive_cutoff,
                )
                if matches:
                    best = matches[0]
                    # Verify: same length AND first char matches
                    # Tighter guard: require first char match to prevent
                    # phantom codes (e.g., FIRMA→MNO, AZY→XYZ)
                    len_ok = abs(len(variant) - len(best)) <= 1
                    char_ok = variant[0] == best[0]
                    # For 3-char codes, also require at least 2 chars in common
                    if len(best) <= 3:
                        common = sum(1 for a, b in zip(variant, best) if a == b)
                        char_ok = char_ok and common >= 2
                    if len_ok and char_ok:
                        qty = self._extract_qty_with_ocr_decode(text, token)
                        logger.info(f"OCR variant fuzzy: '{token}' → '{variant}' → '{best}' ({self.product_catalog[best]})")
                        return {
                            "code": best,
                            "product": self.product_catalog[best],
                            "quantity": qty,
                            "unit": "Piece",
                            "confidence": confidence,
                            "needs_review": True,
                            "match_type": "fuzzy_fallback",
                            "raw_text": text,
                        }
                    else:
                        logger.debug(f"    Rejected fuzzy: '{variant}' → '{best}' (len/char mismatch)")

        return None

    def _extract_quantity_from_text(self, text: str) -> float:
        """
        Extract quantity from text using clear digit patterns only.
        Does NOT attempt OCR letter-to-digit decoding (that's context-dependent).
        Returns 1.0 as default if no number found.
        """
        # First, try to find quantity after a dash (receipt format: CODE - Nqt)
        dash_qty = re.search(r'[-\u2013\u2014]\s*(\d{1,3})', text)
        if dash_qty:
            num = int(dash_qty.group(1))
            if 1 <= num <= 999:
                return float(num)

        tokens = re.split(r'[\s]+', text)
        qty_candidates = []

        for token in tokens:
            # Skip tokens that are clearly product codes (2-4 pure letters)
            if re.match(r'^[A-Za-z]{2,4}$', token):
                continue
            # Skip noise-only tokens (all punctuation)
            if not any(c.isalnum() for c in token):
                continue
            # Skip QT marker tokens (4T, Q1, QT, &T, etc.)
            if re.match(r'^[Qq&4][Tt1I]?$', token):
                continue
            # Skip mixed alpha-digit tokens of 3+ chars (likely product codes)
            if len(token) >= 3 and any(c.isalpha() for c in token) and any(c.isdigit() for c in token):
                continue

            # Extract standalone digits from the token
            digits = re.findall(r'\d+', token)
            for d in digits:
                num = int(d)
                if 1 <= num <= 999:
                    qty_candidates.append(num)

        if qty_candidates:
            # Prefer the LAST candidate (typically follows the code in L→R reading)
            # but filter out obvious noise: if multiple candidates differ wildly,
            # prefer the one that's a single or double digit (typical qty range)
            reasonable = [q for q in qty_candidates if q <= 99]
            if reasonable:
                return float(reasonable[-1])
            return float(qty_candidates[-1])

        # Last resort: try OCR digit decoding on mixed tokens (I0→10, lO→10, etc.)
        for token in tokens:
            if re.match(r'^[A-Za-z]{2,4}$', token):
                continue  # skip product codes
            # Check if token looks like an OCR-confused number (mix of digits + I/l/O/o)
            if re.match(r'^[IlOo|!0-9]+$', token) and any(c in token for c in 'IlOo|!'):
                decoded = ''
                for ch in token:
                    if ch.isdigit():
                        decoded += ch
                    elif ch in ('I', 'i', 'l', '|', '!'):
                        decoded += '1'
                    elif ch in ('O', 'o'):
                        decoded += '0'
                if decoded:
                    try:
                        num = int(decoded)
                        if 1 <= num <= 999:
                            return float(num)
                    except ValueError:
                        pass

        return 1.0

    def _extract_qty_with_ocr_decode(self, text: str, code_token: str) -> float:
        """
        Extract quantity from text with OCR letter-to-digit decoding.
        Only decodes tokens that appear AFTER the product code and look like
        mangled quantity suffixes (e.g., 'Io1f' → 10, 'Iq' → 1).

        Args:
            text: Full line text
            code_token: The product code token found in this line
        """
        # First try clear digits
        clear_qty = self._extract_quantity_from_text(text)
        if clear_qty != 1.0:
            return clear_qty

        # Find tokens after the code and try OCR decoding on them
        tokens = re.split(r'[\s]+', text)
        found_code = False
        for token in tokens:
            if not found_code:
                # Check if this token contains the code
                if code_token.upper() in token.upper() or token.upper() in code_token.upper():
                    found_code = True
                continue

            # Token is after the code — try dash-fragment decoding first
            if token.startswith('-') or token.startswith('\u2013') or token.startswith('\u2014'):
                decoded = self._decode_qty_from_dash_fragment(token)
                if decoded is not None and 1 <= decoded <= 999:
                    return float(decoded)

            # Try OCR qty decoding
            decoded = self._decode_qty_from_ocr_token(token)
            if decoded is not None and 1 <= decoded <= 999:
                return float(decoded)

        # If code was the last token, look BEFORE it too (format: Nqt CODE or V CODE)
        if not found_code or clear_qty == 1.0:
            for token in tokens:
                if code_token.upper() in token.upper() or token.upper() in code_token.upper():
                    break
                # Try dash fragment
                if token.startswith('-') or token.startswith('\u2013') or token.startswith('\u2014'):
                    decoded = self._decode_qty_from_dash_fragment(token)
                    if decoded is not None and 1 <= decoded <= 999:
                        return float(decoded)
                decoded = self._decode_qty_from_ocr_token(token)
                if decoded is not None and 1 <= decoded <= 999:
                    return float(decoded)

        # Last resort: try single-char handwriting digit mapping on ALL tokens
        # This catches cases like 'V JkL' where V is a handwritten '2'
        # Only apply AFTER finding the code token to prevent misinterpreting
        # product code letters as quantity digits
        handwriting_single_char = {
            'V': 2, 'v': 2, 'Z': 2, 'z': 2,
            'S': 5, 's': 5, 'B': 8, 'b': 8,
            'q': 9, 'g': 9,
        }
        found_code_token = False
        for token in tokens:
            token_clean = token.strip()
            # Track when we've passed the code token
            if code_token.upper() in token_clean.upper() or token_clean.upper() in code_token.upper():
                found_code_token = True
                continue
            # Only consider single-character tokens AFTER the code token
            # (or before if code was not found — last resort)
            if len(token_clean) == 1 and token_clean in handwriting_single_char:
                if not found_code_token:
                    continue  # Skip chars before the code — likely part of code context
                # Skip if this char matches a single-letter catalog code or is first letter of one
                upper_ch = token_clean.upper()
                is_catalog_prefix = any(
                    c.startswith(upper_ch) for c in self.product_catalog
                ) if hasattr(self, 'product_catalog') else False
                if not is_catalog_prefix:
                    val = handwriting_single_char[token_clean]
                    if 1 <= val <= 99:
                        logger.debug(f"    Handwriting single-char qty: '{token_clean}' → {val}")
                        return float(val)

        return 1.0

    def _decode_qty_from_ocr_token(self, token: str) -> Optional[int]:
        """
        Decode a quantity from an OCR-mangled token.
        Only decodes tokens that look like they SHOULD be numbers
        (start with a digit or digit-like char like I/O/l).

        Examples:
            'Io1f' → 10  (I→1, o→0, then stop at non-digit 'f')
            '3'    → 3
            '10'   → 10
            'Iq'   → 1   (I→1, q is 'qt' suffix → just 1)
        Does NOT decode product codes like '6n1', 'ALC', 'MNo'.
        """
        # Characters that look like digits (for QUANTITY context only)
        # NOTE: 'q' and 'g' are NOT here because in multi-char tokens
        # they're almost always part of 'qt' suffix, not digit '9'
        qty_letter_to_digit = {
            'O': '0', 'o': '0',
            'I': '1', 'l': '1', '|': '1', '!': '1',
        }

        clean = token.replace('.', '').replace(',', '').replace('_', '').strip()
        if not clean or len(clean) > 6:
            return None

        # Token must start with a digit or a digit-like char (I, O, l, |)
        first_ch = clean[0]
        if not first_ch.isdigit() and first_ch not in qty_letter_to_digit:
            return None

        # Build digit string from the start, stopping at non-digit non-qty-letter chars
        # This handles 'Io1f' → '10' (stops at 'f'), '10qt' → '10' (stops at 'q')
        digit_str = ''
        for ch in clean:
            if ch.isdigit():
                digit_str += ch
            elif ch in qty_letter_to_digit:
                digit_str += qty_letter_to_digit[ch]
            elif ch.lower() in ('q', 't', 'f', 'k', 'x'):
                # Likely 'qt' suffix or noise — stop here
                break
            else:
                # Unknown char — stop building the number
                break

        if digit_str:
            try:
                val = int(digit_str)
                if 1 <= val <= 999:
                    # If the value seems too large for a typical quantity,
                    # try shorter prefixes (e.g., '101' → try '10' first)
                    if val > 99 and len(digit_str) > 2:
                        for prefix_len in range(2, len(digit_str)):
                            prefix_val = int(digit_str[:prefix_len])
                            if 1 <= prefix_val <= 99:
                                return prefix_val
                    return val
            except ValueError:
                pass
        return None

    def _decode_qty_from_dash_fragment(self, token: str) -> Optional[int]:
        """
        Decode quantity from a dash-prefixed fragment.
        In handwritten receipts, 'CODE - Nqt' format, the part after dash
        is the quantity followed by 'qt' suffix.
        '-axk' could be '-2qt' where handwriting '2' was read as 'a'.
        '-3qt' → 3
        """
        # Remove the leading dash
        after_dash = re.sub(r'^[-\u2013\u2014]+\s*', '', token)
        if not after_dash:
            return None

        # Try direct digit extraction first
        digits = re.findall(r'\d+', after_dash)
        if digits:
            try:
                return int(digits[0])
            except ValueError:
                pass

        # Handwriting-specific: first char after dash is likely the quantity digit
        # Common handwriting confusions for digits:
        handwriting_digit_map = {
            'a': 2, 'A': 2,
            'z': 2, 'Z': 2,
            'l': 1, 'I': 1, 'i': 1,
            'o': 0, 'O': 0,
            's': 5, 'S': 5,
            'b': 6, 'B': 8,
            'g': 9, 'q': 9,
            'T': 7,
            "'": 3, '`': 1,   # apostrophe/backtick digit confusion
            '}': 3, '{': 1,   # curly braces resemble 3 and 1
        }

        # Take the first character after dash as the potential digit
        first_char = after_dash[0]
        if first_char in handwriting_digit_map:
            val = handwriting_digit_map[first_char]
            if 1 <= val <= 99:
                # Check if next char(s) look like 'qt' suffix or noise
                # p ≈ Q, y ≈ Q, r ≈ T in sloppy handwriting
                rest = after_dash[1:]
                if not rest or re.match(r'^[qtfxkpyr}\s.\'`]+$', rest, re.IGNORECASE):
                    return val

        # Try OCR decode
        return self._decode_qty_from_ocr_token(after_dash)

    def _identify_code_and_quantity(
        self, groups: Tuple[str, ...]
    ) -> Tuple[Optional[str], Optional[float]]:
        """Determine which group is the product code and which is the quantity."""
        if len(groups) != 2:
            return None, None

        g1, g2 = groups

        def _try_as_qty(s: str) -> Optional[float]:
            """Try to interpret a string as a quantity, with OCR digit decoding."""
            # Direct numeric
            try:
                return float(s)
            except (ValueError, TypeError):
                pass
            # OCR digit decoding: I→1, O→0, l→1, o→0, |→1, !→1
            decoded = ''
            for ch in s:
                if ch.isdigit():
                    decoded += ch
                elif ch in ('I', 'i', 'l', '|', '!'):
                    decoded += '1'
                elif ch in ('O', 'o'):
                    decoded += '0'
                else:
                    return None  # unexpected char — not a quantity
            if decoded:
                try:
                    val = float(decoded)
                    if 1 <= val <= 999:
                        logger.debug(f"    OCR qty decode: '{s}' → {val}")
                        return val
                except (ValueError, TypeError):
                    pass
            return None

        def _looks_like_code(s: str) -> bool:
            """Check if string looks like a product code (starts with 2+ letters)."""
            return bool(re.match(r'^[A-Za-z]{2,}', s))

        try:
            # First group looks like a code → second is quantity
            if _looks_like_code(g1) and not _looks_like_code(g2):
                qty = _try_as_qty(g2)
                if qty is not None:
                    return g1, qty
                return g1, float(g2)
            elif _looks_like_code(g2) and not _looks_like_code(g1):
                qty = _try_as_qty(g1)
                if qty is not None:
                    return g2, qty
                return g2, float(g1)
            elif g1.isalpha():
                qty = _try_as_qty(g2)
                if qty is not None:
                    return g1, qty
                return g1, float(g2)
            elif g2.isalpha():
                qty = _try_as_qty(g1)
                if qty is not None:
                    return g2, qty
                return g2, float(g1)
            else:
                # Both mixed; try to find the code
                if re.match(r"^[A-Za-z]+$", g1):
                    qty = _try_as_qty(g2)
                    if qty is not None:
                        return g1, qty
                    return g1, float(g2)
                elif re.match(r"^[A-Za-z]+$", g2):
                    qty = _try_as_qty(g1)
                    if qty is not None:
                        return g2, qty
                    return g2, float(g1)
        except (ValueError, TypeError):
            pass

        return None, None

    def _map_product_code(self, code: str) -> Tuple[str, str, Optional[str]]:
        """
        Map a product code to its full name using exact, fuzzy, and
        OCR-variant matching to handle handwriting OCR errors like
        6n|→GNI, ALC→ABC, JIL→JKL, deF→DEF.

        Args:
            code: Product code string (uppercase).

        Returns:
            Tuple of (product_name, match_type, matched_code) where
            matched_code is the actual catalog code that was matched.
        """
        # ── Fast path: exact match (no cache needed) ──
        if code in self.product_catalog:
            return self.product_catalog[code], "exact", code

        # ── Trailing O/I ambiguity: PEPW1O could be PEPW10 (exact) OR PEPW1+noise ──
        # If code ends with O or I (common OCR confusion for 0/1),
        # check if stripping the trailing char gives a shorter catalog match.
        # Use scoring to decide: trailing O after a digit strongly suggests
        # the digit-replacement (PEPW1O → PEPW10). Trailing O/I after a
        # letter is more likely noise (e.g., ABCO → ABC + noise O).
        if len(code) >= 4 and code[-1] in ('O', 'I'):
            stripped = code[:-1]
            reverse_digit = '0' if code[-1] == 'O' else '1'
            with_digit = code[:-1] + reverse_digit
            has_long = with_digit in self.product_catalog
            has_short = stripped in self.product_catalog

            if has_long and has_short:
                # AMBIGUOUS: both PEPW1 and PEPW10 exist in catalog.
                # Score each interpretation:
                #   - If the char before the trailing O/I is a digit, it's
                #     more likely part of a number (PEPW1O → PEPW10).
                #   - If the char before is a letter, the trailing O/I is
                #     more likely OCR noise on the shorter code (ABCO → ABC).
                char_before = code[-2] if len(code) >= 2 else ''
                if char_before.isdigit():
                    # e.g., PEPW1O: '1' before 'O' → strongly suggests PEPW10
                    logger.debug(
                        f"    Trailing-O/I ambiguity: '{code}' → '{with_digit}' "
                        f"(digit before O/I, both {stripped} and {with_digit} exist)"
                    )
                    # Return longer match but flag for review
                    return self.product_catalog[with_digit], "ambiguous_oi", with_digit
                else:
                    # e.g., ABCO: letter before 'O' → likely ABC + noise
                    logger.debug(
                        f"    Trailing-O/I ambiguity: '{code}' → '{stripped}' "
                        f"(letter before O/I, preferring shorter code)"
                    )
                    return self.product_catalog[stripped], "ambiguous_oi", stripped
            elif has_long:
                return self.product_catalog[with_digit], "exact", with_digit
            elif has_short:
                # Only shorter exists: PEPW1O with no PEPW10 in catalog → PEPW1
                logger.debug(f"    Trailing-O/I strip: '{code}' → '{stripped}' (only shorter exists)")
                return self.product_catalog[stripped], "exact", stripped

        # ── Check code-match cache before expensive fuzzy/overlap search ──
        if code in self._code_match_cache:
            return self._code_match_cache[code]

        # Try OCR-substituted variants for exact match
        variants = self._generate_ocr_variants(code)
        for variant in variants:
            if variant in self.product_catalog:
                logger.info(f"OCR variant exact: '{code}' → '{variant}'")
                result = (self.product_catalog[variant], "exact", variant)
                self._cache_code_result(code, result)
                return result

        # Fuzzy match on original + all variants — find BEST match across all
        from difflib import SequenceMatcher as _SM
        best_fuzzy_match = None
        best_fuzzy_ratio = 0.0
        best_fuzzy_variant = None
        for variant in variants:
            from app.config import get_adaptive_fuzzy_cutoff as _gafc2
            matches = get_close_matches(
                variant,
                self.product_catalog.keys(),
                n=FUZZY_MAX_RESULTS,
                cutoff=_gafc2(len(variant)),
            )
            for match in matches:
                ratio = _SM(None, variant, match).ratio()
                if ratio > best_fuzzy_ratio:
                    best_fuzzy_ratio = ratio
                    best_fuzzy_match = match
                    best_fuzzy_variant = variant
        if best_fuzzy_match:
            logger.info(
                f"Fuzzy match: '{code}' (variant '{best_fuzzy_variant}') → '{best_fuzzy_match}' "
                f"({self.product_catalog[best_fuzzy_match]}) [ratio={best_fuzzy_ratio:.3f}]"
            )
            result = (self.product_catalog[best_fuzzy_match], "fuzzy", best_fuzzy_match)
            self._cache_code_result(code, result)
            return result

        # Ultra-aggressive: try character overlap matching
        # Uses position-aware scoring: chars matching at the same index score higher
        best_score = 0.0
        best_code = None
        positional_matches_best = 0
        for variant in variants:
            for catalog_code in self.product_catalog:
                # Length guard: reject matches with >1 char length difference
                if abs(len(variant) - len(catalog_code)) > 1:
                    continue
                # Position-aware scoring: count chars matching at same index
                min_len = min(len(variant), len(catalog_code))
                positional_matches = sum(
                    1 for i in range(min_len) if variant[i] == catalog_code[i]
                )
                total = max(len(variant), len(catalog_code))
                score = positional_matches / total if total > 0 else 0
                # Substring containment bonus (still useful)
                if catalog_code in variant or variant in catalog_code:
                    score = max(score, 0.7)
                if score > best_score:
                    best_score = score
                    best_code = catalog_code
                    positional_matches_best = positional_matches

        if best_code and best_score >= 0.85 and positional_matches_best >= 2:
            logger.info(
                f"Aggressive match: '{code}' → '{best_code}' "
                f"(score={best_score:.2f}, {self.product_catalog[best_code]})"
            )
            result = (self.product_catalog[best_code], "fuzzy", best_code)
            self._cache_code_result(code, result)
            return result

        logger.warning(f"Unknown product code: '{code}'")
        miss = ("UNKNOWN PRODUCT", "unknown", None)
        self._cache_code_result(code, miss)
        return miss

    def _get_product_info(self, code: str) -> Optional[Dict]:
        """Get full product info if code is in a detailed catalog."""
        # This will be enhanced when using the database service
        if code in self.product_catalog:
            return {"name": self.product_catalog[code]}
        return None

    # Patterns that look like mangled quantity suffixes, NOT product codes
    # e.g., Iqt (1qt), Io1f (10qt.), Iq (1q)
    QTY_SUFFIX_LIKE_RE = re.compile(
        r'^[IlOo|1][qQtTfF0-9][qQtTfF0-9.]*$'  # starts digit-like, then qt/f suffix
    )

    # Words/tokens that should never be treated as product codes
    NOISE_CODE_RE = re.compile(
        r'^(qt[sy]?\.?|iqt|qty|pcs?|nos?|units?)$', re.IGNORECASE
    )

    def _split_multi_product_line(self, text: str) -> Optional[List[str]]:
        """
        Check if a line contains multiple catalog product codes.
        If so, split it into sub-lines, each containing one code with its context.

        Example: '9 10 XYZ RST 6' with codes XYZ and RST
            → ['9 10 XYZ', 'RST 6']
        """
        text_upper = text.upper()
        positions = []

        for code in self.product_catalog:
            # Find code as a word-boundary match (not part of a longer word)
            for m in re.finditer(r'(?<![A-Za-z])' + re.escape(code) + r'(?![A-Za-z])', text_upper):
                positions.append((m.start(), m.end(), code))

        if len(positions) < 2:
            return None  # Single or no product code — no splitting needed

        # Sort by position in the string
        positions.sort(key=lambda x: x[0])

        # Remove overlapping matches (keep the first occurrence)
        filtered = [positions[0]]
        for pos in positions[1:]:
            if pos[0] >= filtered[-1][1]:
                filtered.append(pos)
        positions = filtered

        if len(positions) < 2:
            return None

        # Split: each sub-line runs from just before its code to just before the next code
        sub_lines = []
        for i, (start, end, code) in enumerate(positions):
            if i == 0:
                line_start = 0
            else:
                # Find the split point: last space before this code
                # Include any leading numbers (row numbers, quantities) with this code
                # by going back to find a natural break point
                prev_end = positions[i - 1][1]
                # Find text between previous code end and this code start
                between = text[prev_end:start]
                # Split point is right after previous code's context ends
                # (after any digits/spaces that follow the previous code)
                line_start = start
                # Walk backward to include any number right before this code
                while line_start > prev_end and text[line_start - 1] in ' \t':
                    line_start -= 1
                while line_start > prev_end and text[line_start - 1].isdigit():
                    line_start -= 1
                while line_start > prev_end and text[line_start - 1] in ' \t':
                    line_start -= 1

            if i + 1 < len(positions):
                # End before the next code's context starts
                next_start = positions[i + 1][0]
                # Walk backward from next code to exclude its leading number
                line_end = next_start
                temp = next_start
                while temp > end and text[temp - 1] in ' \t':
                    temp -= 1
                while temp > end and text[temp - 1].isdigit():
                    temp -= 1
                if temp > end:
                    line_end = temp
            else:
                line_end = len(text)

            sub = text[line_start:line_end].strip()
            if sub:
                sub_lines.append(sub)

        logger.debug(f"  Multi-product split: {[p[2] for p in positions]} from {text!r}")
        return sub_lines if len(sub_lines) >= 2 else None

    def _should_skip(self, text: str) -> bool:
        """Check if a text line should be skipped (header/footer/noise)."""
        if not text or len(text.strip()) < 1:
            return True

        # Skip single non-alphanumeric characters (OCR noise like "&", "|")
        stripped = text.strip()
        if len(stripped) == 1 and not stripped.isalnum():
            return True

        # Skip standalone numbers (1-2 digits) — these are pre-printed S.No
        # from empty rows in boxed receipt templates (e.g., "6", "7", "10")
        if re.match(r'^\s*\d{1,2}\s*$', stripped):
            logger.debug(f"  SKIP standalone number (empty row S.No): {stripped!r}")
            return True

        # Skip tokens that look like mangled qty suffixes: Iqt, Io1f, etc.
        if self.QTY_SUFFIX_LIKE_RE.match(stripped):
            logger.debug(f"  SKIP qty-suffix-like: {stripped!r}")
            return True

        # Skip "Total Items" footer BEFORE checking total lines
        # (so "Total Items 5" doesn't get misidentified as bill total)
        for pattern in self.SKIP_PATTERNS:
            if pattern.search(text):
                return True

        # Skip bill total lines from item parsing (they are already captured in
        # the PRE-SCAN loop above, so we must NOT parse them as items).
        if self._is_total_line(text):
            return True

        # Skip grand total lines (monetary total)
        if self._GRAND_TOTAL_KEYWORD_RE.search(text):
            return True

        return False

    # Lines that contain "total" but are NOT bill totals (e.g., "Total Items: 5")
    _TOTAL_EXCLUDE_RE = re.compile(
        r"(?:total|totd)\s*(?:items|count|no\.?|number|products|entries)",
        re.IGNORECASE,
    )

    def _is_total_line(self, text: str) -> bool:
        """Check if this line is a bill 'Total' line (Total Qty, Grand Total, etc.)
        
        Excludes footer labels like 'Total Items: 5' which are item counts,
        not quantity totals.
        """
        if not self._TOTAL_KEYWORD_RE.search(text):
            return False
        # Reject "Total Items", "Total Count", etc. — these are NOT bill totals
        if self._TOTAL_EXCLUDE_RE.search(text):
            return False
        return True

    def _extract_total_from_line(self, text: str) -> tuple:
        """
        Extract total value from a total line.

        Returns:
            (total_value, raw_text) or (None, text) if no number found.
        """
        for pattern in self.TOTAL_LINE_PATTERNS:
            m = pattern.search(text)
            if m:
                try:
                    val = float(m.group(1))
                    if 0 < val <= 99999:
                        return val, text
                except (ValueError, TypeError):
                    pass

        # Fallback: only extract a number if the line looks like "Total<sep><number>"
        # (i.e., the number follows the total keyword with only separator chars between)
        # Avoid extracting numbers from "Total Items 5" or other unrelated contexts.
        # Accept underscores and dots as separators (common OCR artifacts)
        fallback_match = re.search(
            r'(?:total|totai|tota1|t0tal|totd|subtotal|sub\s*total|grand\s*total|sum)'
            r'\s*(?:qty|quantity|qtv|qly)?[\s:=\-_\.]*(\d+\.?\d*)',
            text, re.IGNORECASE
        )
        if fallback_match:
            try:
                val = float(fallback_match.group(1))
                if 0 < val <= 99999:
                    return val, text
            except (ValueError, TypeError):
                pass

        return None, text

    def _resolve_duplicate_ambiguity(self, items: List[Dict]) -> List[Dict]:
        """
        Resolve ambiguous duplicate codes caused by OCR O/1 confusion.

        Handles two scenarios:
        1. DUPLICATES: OCR reads "PEPW1" as "PEPW1O" → matched to PEPW10.
           If PEPW10 appears twice in items, reassign the ambiguous one to PEPW1.
        2. AMBIGUOUS MATCH TYPE: Items with match_type="ambiguous_oi" that need
           review — if the same code appears twice AND the shorter code is absent,
           reassign one instance to the shorter code.
        """
        from collections import Counter

        code_counts = Counter(item["code"] for item in items)
        duplicated = {code for code, count in code_counts.items() if count > 1}

        # ── Phase 1: Resolve O/I duplicates ──
        for dup_code in duplicated:
            if len(dup_code) < 3:
                continue

            shorter = dup_code[:-1]
            if shorter not in self.product_catalog:
                continue
            if shorter in code_counts:
                # The shorter code already exists in items — no ambiguity
                continue

            # Find all items with this duplicate code
            dup_items = [i for i in items if i["code"] == dup_code]

            # Identify the one most likely to be the shorter code:
            # Priority 1: Check for "ambiguous_oi" match_type
            # Priority 2: Check raw_text for trailing O/I
            reassigned = False
            for item in dup_items:
                if item.get("match_type") == "ambiguous_oi":
                    old_code = item["code"]
                    item["code"] = shorter
                    item["product"] = self.product_catalog[shorter]
                    item["match_type"] = "oi_resolved"
                    item["needs_review"] = True
                    logger.info(
                        f"  Dedup ambiguity (match_type): '{old_code}' → '{shorter}'"
                    )
                    reassigned = True
                    break

            if not reassigned:
                for item in dup_items:
                    raw = item.get("raw_text", "").upper()
                    if (f"{shorter}O" in raw or f"{shorter}I" in raw) and f"{dup_code}" not in raw.replace(f"{shorter}O", "").replace(f"{shorter}I", ""):
                        old_code = item["code"]
                        item["code"] = shorter
                        item["product"] = self.product_catalog[shorter]
                        item["match_type"] = "ocr_variant"
                        logger.info(
                            f"  Dedup ambiguity (raw_text): '{old_code}' → '{shorter}' "
                            f"(raw={raw!r})"
                        )
                        break

        # ── Phase 2: Flag ambiguous_oi items for review ──
        for item in items:
            if item.get("match_type") == "ambiguous_oi":
                item["needs_review"] = True

        return items

    def _aggregate_duplicates(self, items: List[Dict]) -> List[Dict]:
        """
        Aggregate items with the same product code (sum quantities).
        Recalculates line_total when merging.  Flags aggregated items for review.
        """
        seen = {}
        aggregated = []

        for item in items:
            code = item["code"]
            if code in seen:
                # Add quantity to existing item
                idx = seen[code]
                aggregated[idx]["quantity"] += item["quantity"]
                aggregated[idx]["needs_review"] = True
                aggregated[idx]["raw_text"] += f" | {item['raw_text']}"
                # Preserve higher confidence from the better detection
                if item.get("confidence", 0) > aggregated[idx].get("confidence", 0):
                    aggregated[idx]["confidence"] = item["confidence"]
                # If the existing item has no price but the new one does,
                # adopt the new item's price data (prevents losing OCR prices)
                existing_rate = aggregated[idx].get("unit_price", 0)
                new_rate = item.get("unit_price", 0)
                if existing_rate == 0 and new_rate > 0:
                    aggregated[idx]["unit_price"] = new_rate
                # Recalculate line_total after qty merge using best available rate
                rate = aggregated[idx].get("unit_price", 0)
                if rate > 0:
                    aggregated[idx]["line_total"] = round(
                        aggregated[idx]["quantity"] * rate, 2
                    )
                logger.info(
                    f"Aggregated duplicate '{code}': "
                    f"qty now = {aggregated[idx]['quantity']}"
                )
            else:
                seen[code] = len(aggregated)
                aggregated.append(item.copy())

        return aggregated

    def _generate_receipt_number(self) -> str:
        """Generate a unique receipt number with random suffix to prevent collisions."""
        import uuid
        now = datetime.now()
        suffix = uuid.uuid4().hex[:5].upper()
        return f"REC-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}-{suffix}"

    def _cache_code_result(self, code: str, result: Tuple[str, str, Optional[str]]) -> None:
        """Store a code→result mapping, evicting oldest if over limit."""
        if len(self._code_match_cache) >= self._CODE_CACHE_MAX:
            # Evict oldest entry (first key inserted)
            oldest = next(iter(self._code_match_cache))
            del self._code_match_cache[oldest]
        self._code_match_cache[code] = result

    def update_catalog(self, catalog: Dict[str, str]) -> None:
        """Update the product catalog and invalidate match cache."""
        self.product_catalog = {k.upper(): v for k, v in catalog.items()}
        self._code_match_cache.clear()
