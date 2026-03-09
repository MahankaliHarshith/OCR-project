"""
Receipt Processing Service.
Orchestrates the full receipt scanning pipeline:
    Image Capture → Preprocessing → OCR → Parsing → Storage
"""

import logging
import time
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from app.config import UPLOAD_DIR
from app.database import db
from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.engine import get_ocr_engine
from app.ocr.hybrid_engine import get_hybrid_engine
from app.ocr.parser import ReceiptParser
from app.services.product_service import product_service

logger = logging.getLogger(__name__)


class ReceiptService:
    """
    Service orchestrating end-to-end receipt processing.
    """

    def __init__(self):
        self.preprocessor = ImagePreprocessor()
        self.hybrid_engine = get_hybrid_engine()
        self.db = db
        self._parser: Optional[ReceiptParser] = None
        self._catalog_last_refresh: float = 0.0   # epoch seconds
        self._CATALOG_TTL: float = 30.0            # refresh at most once per 30s

    @property
    def parser(self) -> ReceiptParser:
        """Lazy-initialized parser with current product catalog."""
        if self._parser is None:
            catalog = product_service.get_product_code_map()
            self._parser = ReceiptParser(catalog)
        return self._parser

    def refresh_catalog(self) -> None:
        """Refresh the parser's product catalog from the database.

        Rate-limited to once per 30 seconds so rapid sequential scans
        don't each pay a DB round-trip when the catalog hasn't changed.
        Force a refresh by calling with force=True or by setting
        _catalog_last_refresh = 0.
        """
        now = time.time()
        if now - self._catalog_last_refresh < self._CATALOG_TTL:
            return  # catalog is still fresh
        catalog = product_service.get_product_code_map()
        if self._parser:
            self._parser.update_catalog(catalog)
        else:
            self._parser = ReceiptParser(catalog)
        self._catalog_last_refresh = now

    def process_receipt(self, image_path: str) -> Dict:
        """
        Process a single receipt image through the full pipeline.

        Args:
            image_path: Path to the receipt image.

        Returns:
            Structured receipt data with items, confidence scores,
            and processing metadata.
        """
        total_start = time.time()
        self.refresh_catalog()
        logger.info(f"Starting receipt processing pipeline for: {image_path}")

        result = {
            "success": False,
            "receipt_data": None,
            "metadata": {},
            "errors": [],
        }

        # ─── Step 0: Early cache check (skip preprocessing on cache hits) ──────
        # Compute hash of the raw upload BEFORE any heavy OpenCV work.
        # If it's a cache hit, we bypass Steps 2+3 entirely (~200-400ms saved).
        _early_cache_key = None
        try:
            from app.ocr.image_cache import get_image_cache
            _early_cache = get_image_cache()
            _early_cache_key = _early_cache.compute_hash(image_path)
            _cached = _early_cache.get(_early_cache_key)
            if _cached is not None:
                logger.info("[Step 0/5] ✅ Early cache HIT — skipping preprocessing + OCR")
                # Still need to save the uploaded image so DB has a path
                saved_path = self._save_uploaded_image(image_path)
                result["metadata"]["image_path"] = saved_path
                result["metadata"]["early_cache_hit"] = True
                # Inject cached OCR result straight into hybrid_result downstream
                result["metadata"]["_cached_hybrid_result"] = _cached
        except Exception as _e:
            logger.debug(f"[Step 0/5] Early cache check failed: {_e}")

        # ─── Step 1: Save uploaded image ─────────────────────────────────
        try:
            logger.debug("[Step 1/5] Saving uploaded image...")
            saved_path = self._save_uploaded_image(image_path)
            result["metadata"]["image_path"] = saved_path
            logger.debug(f"[Step 1/5] Image saved to: {saved_path}")
        except Exception as e:
            if not result["metadata"].get("image_path"):  # not already saved by Step 0
                result["errors"].append(f"Image save failed: {e}")
                logger.error(f"[Step 1/5] Image save failed: {e}", exc_info=True)
                return result

        # ─── Step 2: Preprocess image (skipped on early cache hit) ───────
        # Defaults in case preprocessing is skipped (cache hit path)
        preprocess_ms = 0
        processed_image = None
        _color_img = None   # reused color image to avoid disk re-read in OCR
        processed_path = result["metadata"].get("image_path", "")
        if result["metadata"].get("early_cache_hit"):
            result["metadata"]["processed_image_path"] = processed_path
            logger.debug("[Step 2/5] Skipped (early cache hit)")
        else:
            try:
                logger.debug("[Step 2/5] Preprocessing image...")
                step_start = time.time()
                processed_image, preprocess_meta = self.preprocessor.preprocess(saved_path)
                preprocess_ms = int((time.time() - step_start) * 1000)
                logger.debug(f"[Step 2/5] Preprocessing done in {preprocess_ms}ms")

                # Save processed image
                processed_path = str(
                    UPLOAD_DIR / f"processed_{Path(saved_path).name}"
                )
                self.preprocessor.save_processed_image(processed_image, processed_path)
                logger.debug(f"[Step 2/5] Processed image saved: {processed_path}")

                # Extract the color image for reuse, then pop it (non-serializable)
                _color_img = preprocess_meta.pop("_color_image", None)
                result["metadata"]["preprocessing"] = preprocess_meta
                result["metadata"]["processed_image_path"] = processed_path

            except Exception as e:
                result["errors"].append(f"Preprocessing failed: {e}")
                logger.error(f"[Step 2/5] Preprocessing failed: {e}", exc_info=True)
                return result

        # ─── Step 3: OCR text extraction (Hybrid Engine) ────────────────
        try:
            step_start = time.time()

            # ── Short-circuit: use cached OCR result if Step 0 hit the cache ──
            if result["metadata"].get("_cached_hybrid_result"):
                hybrid_result = result["metadata"].pop("_cached_hybrid_result")
                is_structured = hybrid_result.get("metadata", {}).get("is_structured", False)
                result["metadata"]["receipt_type"] = "structured" if is_structured else "handwritten"
                logger.info("[Step 3/5] ✅ Using early cached OCR result (engine skipped)")
            else:
                # Detect structured / boxed receipt
                is_structured = self.preprocessor.detect_grid_structure(processed_image)
                result["metadata"]["receipt_type"] = "structured" if is_structured else "handwritten"

                # ── Run Hybrid OCR Engine ──
                # The hybrid engine automatically selects the best strategy:
                #   AUTO mode: Azure → EasyOCR fallback
                #   LOCAL mode: EasyOCR multi-pass (original behavior)
                #   AZURE mode: Azure only
                logger.debug(f"[Step 3/5] Running hybrid OCR engine (mode={self.hybrid_engine.mode})...")

                # Pass the already-loaded color image to avoid reloading from disk
                hybrid_result = self.hybrid_engine.process_image(
                    image_path=saved_path,
                    processed_image=processed_image,
                    is_structured=is_structured,
                    original_color=_color_img,
                )

            engine_used = hybrid_result["engine_used"]
            ocr_results = hybrid_result["ocr_detections"]
            azure_structured = hybrid_result.get("azure_structured")
            ocr_ms = hybrid_result["ocr_time_ms"]

            logger.info(
                f"[Step 3/5] Hybrid OCR done: engine={engine_used}, "
                f"{len(ocr_results)} detections, {ocr_ms}ms"
            )

            if not ocr_results and not azure_structured:
                result["errors"].append(
                    "No text detected. Please ensure receipt is clearly visible."
                )
                result["metadata"]["ocr_time_ms"] = ocr_ms
                result["metadata"]["engine_used"] = engine_used
                return result

            result["metadata"]["ocr_time_ms"] = ocr_ms
            result["metadata"]["ocr_detections"] = len(ocr_results)
            result["metadata"]["ocr_avg_confidence"] = hybrid_result["confidence_avg"]
            result["metadata"]["ocr_passes"] = hybrid_result["ocr_passes"]
            result["metadata"]["engine_used"] = engine_used
            result["metadata"]["hybrid_metadata"] = hybrid_result.get("metadata", {})

            # Surface strategy and Azure page usage for frontend alerts
            hybrid_meta = hybrid_result.get("metadata", {})
            result["metadata"]["strategy"] = hybrid_meta.get("strategy", "unknown")
            result["metadata"]["azure_pages_used"] = hybrid_meta.get("azure_pages_used", 0)
            if hybrid_meta.get("reason"):
                result["metadata"]["reason"] = hybrid_meta["reason"]

            result["metadata"]["raw_ocr"] = [
                {"text": r["text"], "confidence": r["confidence"]}
                for r in ocr_results
            ]

        except Exception as e:
            result["errors"].append(f"OCR extraction failed: {e}")
            logger.error(f"OCR extraction failed: {e}", exc_info=True)
            return result

        # ─── Step 4: Parse receipt data ──────────────────────────────────
        try:
            logger.debug("[Step 4/5] Parsing receipt data...")
            step_start = time.time()

            # ── If Azure receipt model returned structured items, use them directly ──
            if azure_structured and azure_structured.get("items"):
                receipt_data = self._parse_azure_structured(
                    azure_structured, ocr_results, is_structured
                )
                logger.info(
                    f"[Step 4/5] Azure structured parse: "
                    f"{receipt_data['total_items']} items"
                )
            else:
                # ── Standard parse: OCR detections → parser (works for both Azure Read & EasyOCR) ──
                receipt_data = self.parser.parse(ocr_results, is_structured=is_structured)

            parse_ms = int((time.time() - step_start) * 1000)
            logger.debug(
                f"[Step 4/5] Parse done in {parse_ms}ms → "
                f"{receipt_data['total_items']} items, status={receipt_data['processing_status']}"
            )

            result["metadata"]["parse_time_ms"] = parse_ms

            if receipt_data["processing_status"] == "no_items_found":
                result["errors"].append(
                    "Could not identify any items on the receipt. "
                    "Please check the image and try again."
                )
                result["receipt_data"] = receipt_data
                logger.warning("[Step 4/5] No items found after parsing")
                return result

        except Exception as e:
            result["errors"].append(f"Data parsing failed: {e}")
            logger.error(f"[Step 4/5] Data parsing failed: {e}", exc_info=True)
            return result

        # ─── Step 5: Save to database ────────────────────────────────────
        try:
            logger.debug("[Step 5/5] Saving to database...")
            receipt_id = self.db.create_receipt(
                receipt_number=receipt_data["receipt_id"],
                image_path=saved_path,
                processed_image_path=processed_path,
            )

            # Add product unit info to items
            catalog_full = product_service.get_product_catalog_full()
            for item in receipt_data["items"]:
                code = item["code"]
                if code in catalog_full:
                    item["unit"] = catalog_full[code].get("unit", "Piece")

            self.db.add_receipt_items(receipt_id, receipt_data["items"])

            # Log processing stages (single batch insert — 1 round-trip)
            total_ms = int((time.time() - total_start) * 1000)
            self.db.add_processing_logs_batch([
                (receipt_id, "preprocessing", "success", preprocess_ms, ""),
                (receipt_id, "ocr_extraction", "success", ocr_ms, ""),
                (receipt_id, "data_parsing", "success", parse_ms, ""),
                (receipt_id, "total_pipeline", "success", total_ms, ""),
            ])

            receipt_data["db_id"] = receipt_id
            result["receipt_data"] = receipt_data
            result["success"] = True
            result["metadata"]["total_time_ms"] = total_ms

            logger.info(
                f"Receipt processed successfully: {receipt_data['receipt_id']} | "
                f"{receipt_data['total_items']} items | "
                f"{total_ms}ms total"
            )

        except Exception as e:
            result["errors"].append(f"Database save failed: {e}")
            logger.error(f"[Step 5/5] Database save failed: {e}", exc_info=True)
            # Return parsed data so user can see OCR results, but mark as partial
            result["receipt_data"] = receipt_data
            result["success"] = False

        return result

    def get_receipt(self, receipt_id: int) -> Optional[Dict]:
        """Get a receipt by ID with all items."""
        return self.db.get_receipt(receipt_id)

    def get_recent_receipts(self, limit: int = 10, offset: int = 0) -> List[Dict]:
        """Get the most recent receipts (paginated)."""
        return self.db.get_recent_receipts(limit, offset)

    def count_receipts(self) -> int:
        """Return total receipt count."""
        return self.db.count_receipts()

    def get_receipts_by_date(self, date: str) -> List[Dict]:
        """Get all receipts for a specific date."""
        return self.db.get_receipts_by_date(date)

    def update_receipt_item(
        self,
        item_id: int,
        product_code: str,
        product_name: str,
        quantity: float,
    ) -> bool:
        """Update a receipt item (manual correction). Returns False if not found."""
        return self.db.update_receipt_item(item_id, product_code, product_name, quantity)

    def delete_receipt(self, receipt_id: int) -> bool:
        """Delete a receipt."""
        return self.db.delete_receipt(receipt_id)

    def add_receipt_item(
        self,
        receipt_id: int,
        product_code: str,
        product_name: str,
        quantity: float,
    ) -> int:
        """Add a new item to an existing receipt (for manually added rows)."""
        return self.db.add_receipt_item(receipt_id, product_code, product_name, quantity)

    def _parse_azure_structured(self, azure_data: Dict, ocr_detections: List[Dict], is_structured: bool = False) -> Dict:
        """
        Convert Azure prebuilt-receipt structured items into our receipt format.

        When Azure's receipt model extracts items with descriptions and quantities,
        we map them to product codes from our catalog using fuzzy matching.
        If Azure items don't map well, we fall back to parsing the raw OCR detections.

        Args:
            azure_data: Structured data from Azure receipt model.
            ocr_detections: Raw OCR detections for fallback parsing.
            is_structured: Whether the receipt has grid structure.

        Returns:
            Standard receipt data dict (same format as parser.parse()).
        """
        from difflib import get_close_matches
        from datetime import datetime

        items = []
        azure_items = azure_data.get("items", [])
        catalog = self.parser.product_catalog
        catalog_names = {v.upper(): k for k, v in catalog.items()}  # reverse: name → code

        for azure_item in azure_items:
            description = azure_item.get("description", "").strip()
            quantity = azure_item.get("quantity", 1.0)
            confidence = azure_item.get("confidence", 0.9)

            if not description:
                continue

            # Try to match description to our product catalog
            desc_upper = description.upper()
            code = None
            product_name = description
            match_type = "azure-receipt"

            # 1. Direct code match (description IS the product code)
            if desc_upper in catalog:
                code = desc_upper
                product_name = catalog[code]
                match_type = "azure-exact"
            else:
                # 2. Check if description contains a product code
                for cat_code in catalog:
                    if cat_code in desc_upper:
                        code = cat_code
                        product_name = catalog[code]
                        match_type = "azure-contains"
                        break

                if not code:
                    # 3. Fuzzy match description against product names
                    close = get_close_matches(
                        desc_upper, catalog_names.keys(), n=1, cutoff=0.5
                    )
                    if close:
                        code = catalog_names[close[0]]
                        product_name = catalog[code]
                        match_type = "azure-fuzzy"
                    else:
                        # 4. Use description as-is (unknown product)
                        code = desc_upper[:6] if len(desc_upper) >= 2 else "UNK"
                        match_type = "azure-unmatched"

            # Look up unit from catalog
            unit = "Piece"
            if code and code in catalog:
                product_info = product_service.get_product(code)
                if product_info:
                    unit = product_info.get("unit", "Piece")

            items.append({
                "code": code,
                "product": product_name,
                "quantity": max(1.0, min(9999.0, float(quantity))),
                "unit": unit,
                "confidence": round(confidence, 4),
                "needs_review": match_type == "azure-unmatched" or confidence < 0.6,
                "match_type": match_type,
                "raw_text": description,
                "y_center": 0,
            })

        # If Azure found very few items, supplement with parser on raw OCR text
        if len(items) < 2 and ocr_detections:
            logger.info(
                f"[Azure Parse] Only {len(items)} items from receipt model, "
                f"supplementing with OCR text parser..."
            )
            parsed = self.parser.parse(ocr_detections, is_structured=is_structured)
            parsed_items = parsed.get("items", [])
            existing_codes = {i["code"] for i in items}
            for pi in parsed_items:
                if pi["code"] not in existing_codes:
                    pi["match_type"] = f"parser-supplement ({pi.get('match_type', '')})"
                    items.append(pi)
                    existing_codes.add(pi["code"])

        # Calculate stats
        avg_confidence = sum(i["confidence"] for i in items) / len(items) if items else 0
        needs_review = any(i["needs_review"] for i in items) or avg_confidence < 0.85

        receipt_number = self.parser._generate_receipt_number()

        return {
            "receipt_id": receipt_number,
            "scan_timestamp": datetime.now().isoformat(),
            "items": items,
            "total_items": len(items),
            "avg_confidence": round(avg_confidence, 4),
            "needs_review": needs_review,
            "unparsed_lines": [],
            "processing_status": "success" if items else "no_items_found",
        }

    def _quick_item_count(self, ocr_results: List[Dict]) -> int:
        """
        Quick-parse OCR results to count how many known catalog items are present.
        Used to decide if a second OCR pass is needed. Much faster than full parse
        because it only checks for catalog code matches, no quantity extraction.
        """
        catalog = self.parser.product_catalog
        found_codes = set()
        for r in ocr_results:
            text = r.get("text", "").upper().strip()
            # Check each token for catalog match (exact or OCR-variant)
            tokens = text.split()
            for token in tokens:
                # Keep alphanumeric chars (not just alpha) so TEW1, PEPW10 match
                clean = ''.join(c for c in token if c.isalnum()).upper()
                if len(clean) < 2 or len(clean) > 7:
                    continue
                # Exact match
                if clean in catalog:
                    found_codes.add(clean)
                    continue
                # OCR variant match
                variants = self.parser._generate_ocr_variants(clean)
                for v in variants:
                    if v in catalog:
                        found_codes.add(v)
                        break
        return len(found_codes)

    def _save_uploaded_image(self, image_path: str) -> str:
        """
        Copy uploaded image to the uploads directory.

        Args:
            image_path: Original image path.

        Returns:
            Path to the saved copy.
        """
        src = Path(image_path)
        if not src.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        import uuid
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:6]
        dest = UPLOAD_DIR / f"receipt_{timestamp}_{unique}{src.suffix}"

        # If source is already in uploads dir, just return it
        if src.parent.resolve() == UPLOAD_DIR.resolve():
            return str(src)

        shutil.copy2(str(src), str(dest))
        logger.debug(f"Image saved to: {dest}")
        return str(dest)


# Singleton
receipt_service = ReceiptService()
