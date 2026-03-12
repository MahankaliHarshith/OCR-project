"""
Azure Document Intelligence OCR Engine.
Cloud-based OCR using Azure's prebuilt receipt model and Read model
for highest accuracy and speed on handwritten + printed receipts.

This module is the PRIMARY engine in the hybrid architecture.
Falls back to local EasyOCR when Azure is unavailable (offline, quota, errors).
"""

import os
import io
import logging
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

try:
    from app.metrics import record_azure_call as _record_azure_call
except Exception:
    def _record_azure_call(model="", success=True):
        pass

from app.tracing import get_tracer, optional_span
_tracer = get_tracer(__name__)

# ── Lazy imports — only loaded when Azure is actually used ──────────────────
_azure_available = None  # None = not checked yet


def _check_azure_available() -> bool:
    """Check if Azure SDK is installed and credentials are configured."""
    global _azure_available
    if _azure_available is not None:
        return _azure_available

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()
        key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()

        if endpoint and key:
            _azure_available = True
            logger.info("Azure Document Intelligence: ✅ SDK installed, credentials configured")
        else:
            _azure_available = False
            logger.info(
                "Azure Document Intelligence: ⚠ SDK installed but credentials not set. "
                "Set AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY "
                "environment variables. Falling back to local EasyOCR."
            )
    except ImportError:
        _azure_available = False
        logger.info(
            "Azure Document Intelligence: ⚠ SDK not installed. "
            "Run: pip install azure-ai-documentintelligence "
            "Falling back to local EasyOCR."
        )

    return _azure_available


def is_azure_available() -> bool:
    """Public check: is Azure Document Intelligence usable?"""
    return _check_azure_available()


class AzureOCREngine:
    """
    OCR engine using Azure Document Intelligence.

    Supports two models:
        - prebuilt-receipt: Structured receipt extraction (items, quantities, prices)
        - prebuilt-read:    General text extraction (handwritten + printed)

    The engine automatically selects the best model based on the receipt type.
    """

    def __init__(self):
        """Initialize Azure Document Intelligence client."""
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        self.endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()
        self.key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()

        if not self.endpoint or not self.key:
            raise ValueError(
                "Azure Document Intelligence credentials not configured. "
                "Set AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY."
            )

        self.client = DocumentIntelligenceClient(
            endpoint=self.endpoint,
            credential=AzureKeyCredential(self.key),
        )

        logger.info(
            f"Azure Document Intelligence client initialized "
            f"(endpoint={self.endpoint[:40]}...)"
        )

    def _optimize_image_for_upload(self, image_path: str) -> bytes:
        """
        Compress and resize image before sending to Azure.

        Saves bandwidth & upload time without hurting accuracy.
        Azure works perfectly at 1500px max dimension + JPEG quality 85.

        Typical savings:
            - 4MB phone photo → 200-400KB optimized
            - 3-5× faster upload, same accuracy
        """
        import cv2
        from app.config import AZURE_IMAGE_MAX_DIMENSION, AZURE_IMAGE_QUALITY

        img = cv2.imread(image_path)
        if img is None:
            # Fallback: read raw bytes if OpenCV can't decode
            with open(image_path, "rb") as f:
                return f.read()

        h, w = img.shape[:2]
        original_size = os.path.getsize(image_path)

        # Resize if larger than max dimension
        max_dim = AZURE_IMAGE_MAX_DIMENSION
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            logger.debug(f"[Azure] Resized {w}x{h} → {img.shape[1]}x{img.shape[0]}")

        # Encode as JPEG
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, AZURE_IMAGE_QUALITY]
        _, buffer = cv2.imencode(".jpg", img, encode_params)
        optimized = buffer.tobytes()

        savings = (1 - len(optimized) / original_size) * 100 if original_size > 0 else 0
        logger.debug(
            f"[Azure] Image optimized: {original_size/1024:.0f}KB → {len(optimized)/1024:.0f}KB "
            f"({savings:.0f}% smaller)"
        )

        return optimized

    def extract_receipt_structured(self, image_path: str) -> Dict:
        """
        Extract structured receipt data using Azure's prebuilt-receipt model.

        This model natively understands receipt layouts and extracts:
        - Merchant name, address, phone
        - Transaction date/time
        - Line items with description, quantity, price, total
        - Subtotal, tax, tip, total

        Args:
            image_path: Path to the receipt image file.

        Returns:
            Dict with structured receipt data and raw OCR detections.
        """
        from app.ocr.usage_tracker import get_usage_tracker

        start = time.time()
        logger.info(f"Azure Receipt extraction starting: {image_path}")

        # Optimize image before upload (saves bandwidth + time)
        with optional_span(_tracer, "azure.optimize_image") as _opt_span:
            image_data = self._optimize_image_for_upload(image_path)
            _opt_span.set_attribute("image.size_bytes", len(image_data))

        success = False
        with optional_span(_tracer, "azure.analyze_document", {"azure.model": "prebuilt-receipt"}) as _api_span:
            try:
                poller = self.client.begin_analyze_document(
                    model_id="prebuilt-receipt",
                    body=image_data,
                    content_type="application/octet-stream",
                )
                result = poller.result()
                success = True
                _api_span.set_attribute("azure.success", True)
            except Exception as e:
                _api_span.set_attribute("azure.success", False)
                _api_span.record_exception(e)
                logger.error(f"Azure Receipt extraction failed: {e}")
                # Track failed call too (Azure still bills for some errors)
                get_usage_tracker().record_call("prebuilt-receipt", pages=1, success=False)
                _record_azure_call(model="prebuilt-receipt", success=False)
                raise

        elapsed_ms = int((time.time() - start) * 1000)

        # Track usage
        get_usage_tracker().record_call("prebuilt-receipt", pages=1, success=success)
        _record_azure_call(model="prebuilt-receipt", success=success)

        # Parse Azure receipt result into our format
        parsed = self._parse_receipt_result(result)
        parsed["azure_time_ms"] = elapsed_ms
        parsed["azure_model"] = "prebuilt-receipt"

        logger.info(
            f"Azure Receipt extraction done in {elapsed_ms}ms: "
            f"{len(parsed.get('items', []))} items, "
            f"{len(parsed.get('ocr_detections', []))} text blocks"
        )

        return parsed

    def extract_text_read(self, image_path: str) -> List[Dict]:
        """
        Extract raw text using Azure's prebuilt-read model.

        Best for handwritten text, messy layouts, or when the receipt model
        doesn't find enough structured items. Returns EasyOCR-compatible
        detection format for seamless integration with the existing parser.

        Args:
            image_path: Path to the image file.

        Returns:
            List of detection dicts compatible with EasyOCR format:
            [{"bbox": [...], "text": "...", "confidence": 0.95, "needs_review": False}]
        """
        from app.ocr.usage_tracker import get_usage_tracker

        start = time.time()
        logger.info(f"Azure Read extraction starting: {image_path}")

        # Optimize image before upload
        with optional_span(_tracer, "azure.optimize_image") as _opt_span:
            image_data = self._optimize_image_for_upload(image_path)
            _opt_span.set_attribute("image.size_bytes", len(image_data))

        success = False
        with optional_span(_tracer, "azure.analyze_document", {"azure.model": "prebuilt-read"}) as _api_span:
            try:
                poller = self.client.begin_analyze_document(
                    model_id="prebuilt-read",
                    body=image_data,
                    content_type="application/octet-stream",
                )
                result = poller.result()
                success = True
                _api_span.set_attribute("azure.success", True)
            except Exception as e:
                _api_span.set_attribute("azure.success", False)
                _api_span.record_exception(e)
                logger.error(f"Azure Read extraction failed: {e}")
                get_usage_tracker().record_call("prebuilt-read", pages=1, success=False)
                _record_azure_call(model="prebuilt-read", success=False)
                raise

        elapsed_ms = int((time.time() - start) * 1000)

        # Track usage
        get_usage_tracker().record_call("prebuilt-read", pages=1, success=success)
        _record_azure_call(model="prebuilt-read", success=success)

        # Convert Azure Read result to EasyOCR-compatible format
        detections = self._convert_read_to_detections(result)

        logger.info(
            f"Azure Read extraction done in {elapsed_ms}ms: "
            f"{len(detections)} text elements"
        )

        return detections

    def extract_text_from_bytes(self, image_bytes: bytes, model: str = "prebuilt-read") -> List[Dict]:
        """
        Extract text from in-memory image bytes.

        Args:
            image_bytes: Raw image bytes (JPEG/PNG).
            model: Azure model to use.

        Returns:
            List of EasyOCR-compatible detection dicts.
        """
        from app.ocr.usage_tracker import get_usage_tracker

        start = time.time()
        success = False

        with optional_span(_tracer, "azure.analyze_document", {"azure.model": model}) as _api_span:
            try:
                poller = self.client.begin_analyze_document(
                    model_id=model,
                    body=image_bytes,
                    content_type="application/octet-stream",
                )
                result = poller.result()
                success = True
                _api_span.set_attribute("azure.success", True)
                _api_span.set_attribute("azure.input_bytes", len(image_bytes))
            except Exception as e:
                _api_span.set_attribute("azure.success", False)
                _api_span.record_exception(e)
                logger.error(f"Azure extraction from bytes failed: {e}")
                get_usage_tracker().record_call(model, pages=1, success=False)
                _record_azure_call(model=model, success=False)
                raise

        elapsed_ms = int((time.time() - start) * 1000)

        # Track usage (was previously missing — silent page consumption)
        get_usage_tracker().record_call(model, pages=1, success=success)
        _record_azure_call(model=model, success=success)

        detections = self._convert_read_to_detections(result)

        logger.info(f"Azure bytes extraction done in {elapsed_ms}ms: {len(detections)} elements")
        return detections

    @staticmethod
    def _get_field_value(field):
        """
        Extract the value from a DocumentField using the correct typed property.

        Azure SDK v1.0+ replaced the generic `.value` with type-specific
        properties: value_string, value_number, value_currency, etc.
        This helper inspects `field.type` and returns the appropriate value.

        For currency fields, returns the `.amount` (float) directly.
        """
        if field is None:
            return None

        field_type = str(field.type).lower() if field.type else ""

        # Map each DocumentFieldType to its typed property
        if "string" in field_type:
            return field.value_string
        elif "currency" in field_type:
            # CurrencyValue has .amount and .currency_code
            return field.value_currency.amount if field.value_currency else None
        elif "number" in field_type:
            return field.value_number
        elif "integer" in field_type:
            return field.value_integer
        elif "date" in field_type:
            return field.value_date
        elif "time" in field_type:
            return field.value_time
        elif "array" in field_type:
            return field.value_array
        elif "object" in field_type:
            return field.value_object
        elif "boolean" in field_type:
            return field.value_boolean
        elif "address" in field_type:
            return field.value_address
        elif "phone" in field_type:
            return field.value_phone_number
        elif "selection" in field_type and "mark" in field_type:
            return field.value_selection_mark
        elif "selection" in field_type and "group" in field_type:
            return field.value_selection_group
        elif "signature" in field_type:
            return field.value_signature
        elif "country" in field_type:
            return field.value_country_region
        else:
            # Fallback: try content (raw text), always available
            return field.content

    def _parse_receipt_result(self, result) -> Dict:
        """
        Parse Azure prebuilt-receipt result into our application format.

        Maps Azure's structured receipt fields to our item/quantity format
        that the existing parser and frontend expect.
        """
        parsed = {
            "items": [],
            "ocr_detections": [],
            "merchant": None,
            "transaction_date": None,
            "total": None,
            "subtotal": None,
            "tax": None,
            "raw_content": "",
        }

        if not result or not result.documents:
            # Fall back to page-level text if no documents parsed
            parsed["ocr_detections"] = self._extract_page_text(result)
            return parsed

        doc = result.documents[0]
        fields = doc.fields if doc.fields else {}

        # ── Extract merchant info ──
        merchant_val = self._get_field_value(fields.get("MerchantName"))
        if merchant_val:
            parsed["merchant"] = merchant_val

        # ── Extract date ──
        date_val = self._get_field_value(fields.get("TransactionDate"))
        if date_val:
            parsed["transaction_date"] = str(date_val)

        # ── Extract totals ──
        for total_field in ["Total", "Subtotal", "TotalTax"]:
            total_val = self._get_field_value(fields.get(total_field))
            if total_val is not None:
                key = total_field.lower()
                try:
                    parsed[key] = float(total_val)
                except (ValueError, TypeError):
                    pass

        # ── Extract line items ──
        items_list = self._get_field_value(fields.get("Items"))
        if items_list:
            for idx, item_field in enumerate(items_list):
                item_data = self._get_field_value(item_field) or {}

                description = ""
                quantity = 1.0
                price = None
                total_price = None
                confidence = item_field.confidence if item_field.confidence else 0.0

                desc_val = self._get_field_value(item_data.get("Description"))
                if desc_val:
                    description = str(desc_val).strip()

                qty_val = self._get_field_value(item_data.get("Quantity"))
                if qty_val is not None:
                    try:
                        quantity = float(qty_val)
                    except (ValueError, TypeError):
                        quantity = 1.0

                price_val = self._get_field_value(item_data.get("Price"))
                if price_val is not None:
                    try:
                        price = float(price_val)
                    except (ValueError, TypeError):
                        pass

                tp_val = self._get_field_value(item_data.get("TotalPrice"))
                if tp_val is not None:
                    try:
                        total_price = float(tp_val)
                    except (ValueError, TypeError):
                        pass

                if description:
                    parsed["items"].append({
                        "description": description,
                        "quantity": quantity,
                        "price": price,
                        "total_price": total_price,
                        "confidence": round(confidence, 4),
                        "source": "azure-receipt-model",
                    })

        # ── Also extract raw text for the parser to use as fallback ──
        parsed["ocr_detections"] = self._extract_page_text(result)

        # Build raw_content string
        lines = []
        for det in parsed["ocr_detections"]:
            lines.append(det["text"])
        parsed["raw_content"] = "\n".join(lines)

        return parsed

    def _extract_page_text(self, result) -> List[Dict]:
        """
        Extract all text blocks from Azure result pages.
        Returns EasyOCR-compatible detection format.
        """
        detections = []

        if not result or not result.pages:
            return detections

        for page in result.pages:
            if not page.lines:
                continue

            for line in page.lines:
                text = line.content.strip() if line.content else ""
                if not text:
                    continue

                # Convert Azure polygon to bbox format
                bbox = self._polygon_to_bbox(line.polygon)

                # Azure lines don't have per-line confidence in Read model,
                # but words do. Average word confidences for the line.
                # Use realistic default (0.80) instead of inflated 1.0.
                line_conf = 0.80
                if page.words:
                    # Find words that belong to this line by checking overlap
                    word_confs = []
                    line_words = set(text.split())
                    for word in page.words:
                        if word.content and word.content.strip() in line_words:
                            if word.confidence is not None:
                                word_confs.append(word.confidence)
                    if word_confs:
                        line_conf = sum(word_confs) / len(word_confs)

                detections.append({
                    "bbox": bbox,
                    "text": text,
                    "confidence": round(line_conf, 4),
                    "needs_review": line_conf < 0.4,
                })

        return detections

    def _convert_read_to_detections(self, result) -> List[Dict]:
        """
        Convert Azure Read model result to EasyOCR-compatible detection list.
        Uses word-level detections for maximum granularity.
        """
        detections = []

        if not result or not result.pages:
            return detections

        for page in result.pages:
            # Use line-level for better grouping (similar to EasyOCR paragraph=False)
            if page.lines:
                for line in page.lines:
                    text = line.content.strip() if line.content else ""
                    if not text:
                        continue

                    bbox = self._polygon_to_bbox(line.polygon)

                    # Get word-level confidence average
                    # Use realistic default (0.80) instead of inflated 0.95.
                    # Azure Read model doesn't always provide per-word confidence,
                    # and defaulting to 0.95 masks potential accuracy issues.
                    line_conf = 0.80
                    if page.words:
                        word_confs = []
                        # Match words to this line using content overlap
                        # Use exact word boundary matching to avoid false substring matches
                        line_words = set(text.split())
                        for word in page.words:
                            if word.content and word.content.strip() in line_words:
                                if word.confidence is not None:
                                    word_confs.append(word.confidence)
                        if word_confs:
                            line_conf = sum(word_confs) / len(word_confs)

                    detections.append({
                        "bbox": bbox,
                        "text": text,
                        "confidence": round(line_conf, 4),
                        "needs_review": line_conf < 0.4,
                    })

        return detections

    def _polygon_to_bbox(self, polygon) -> list:
        """
        Convert Azure polygon points to EasyOCR bbox format.

        Azure returns polygon as [x1,y1, x2,y2, x3,y3, x4,y4] (flat list)
        EasyOCR expects [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
        """
        if not polygon:
            return [[0, 0], [0, 0], [0, 0], [0, 0]]

        # Azure polygon is a flat list of coordinates
        if len(polygon) >= 8:
            return [
                [float(polygon[0]), float(polygon[1])],
                [float(polygon[2]), float(polygon[3])],
                [float(polygon[4]), float(polygon[5])],
                [float(polygon[6]), float(polygon[7])],
            ]
        elif len(polygon) == 4:
            # Sometimes Azure returns 4 Point objects
            # Return clockwise: TL, TR, BR, BL
            return [
                [float(polygon[0]), float(polygon[1])],
                [float(polygon[2]), float(polygon[1])],
                [float(polygon[2]), float(polygon[3])],
                [float(polygon[0]), float(polygon[3])],
            ]
        else:
            return [[0, 0], [0, 0], [0, 0], [0, 0]]


# ─── Lazy singleton ──────────────────────────────────────────────────────────

_azure_engine: Optional[AzureOCREngine] = None


def get_azure_engine() -> Optional[AzureOCREngine]:
    """
    Get or create the Azure OCR engine singleton.
    Returns None if Azure is not available (SDK not installed or no credentials).
    """
    global _azure_engine
    if _azure_engine is not None:
        return _azure_engine

    if not is_azure_available():
        return None

    try:
        _azure_engine = AzureOCREngine()
        return _azure_engine
    except Exception as e:
        logger.warning(f"Failed to initialize Azure OCR engine: {e}")
        return None
