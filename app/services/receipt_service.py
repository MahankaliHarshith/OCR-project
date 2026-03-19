"""
Receipt Processing Service.
Orchestrates the full receipt scanning pipeline:
    Image Capture → Preprocessing → OCR → Parsing → Storage
"""

import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from app.config import UPLOAD_DIR
from app.database import db
from app.ocr.hybrid_engine import get_hybrid_engine
from app.ocr.parser import ReceiptParser
from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.total_verifier import get_total_verifier
from app.services.product_service import product_service

logger = logging.getLogger(__name__)

try:
    from app.metrics import record_scan as _record_scan
except Exception:
    def _record_scan(*a, **kw):
        pass

from app.error_tracking import add_breadcrumb, capture_exception  # noqa: E402
from app.ocr.quality_scorer import quality_scorer  # noqa: E402
from app.ocr.validators import receipt_validator  # noqa: E402
from app.services.correction_service import correction_service  # noqa: E402
from app.services.dedup_service import dedup_service  # noqa: E402
from app.tracing import get_tracer, optional_span  # noqa: E402

_tracer = get_tracer(__name__)


class ReceiptService:
    """
    Service orchestrating end-to-end receipt processing.
    """

    def __init__(self):
        self.preprocessor = ImagePreprocessor()
        self.hybrid_engine = get_hybrid_engine()
        self.db = db
        self._parser: ReceiptParser | None = None
        self._catalog_last_refresh: float = 0.0   # epoch seconds
        self._CATALOG_TTL: float = 30.0            # refresh at most once per 30s
        self._catalog_lock = threading.Lock()       # must be created in __init__, not lazily

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

        Thread-safe: guarded by _catalog_lock to prevent races in
        concurrent batch processing.
        """
        with self._catalog_lock:
            now = time.time()
            if now - self._catalog_last_refresh < self._CATALOG_TTL:
                return  # catalog is still fresh
            catalog = product_service.get_product_code_map()
            if self._parser:
                self._parser.update_catalog(catalog)
            else:
                self._parser = ReceiptParser(catalog)
            self._catalog_last_refresh = now

    def process_receipt(self, image_path: str) -> dict:
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

        # Start a root span for the entire pipeline
        _pipeline_span = None
        try:
            from opentelemetry import trace as _otrace
            _pipeline_span = _tracer.start_span(
                "process_receipt",
                attributes={"receipt.image_path": str(image_path)},
            )
            _ctx = _otrace.context_api.set_span_in_context(_pipeline_span)
            _token = _otrace.context_api.attach(_ctx)
        except Exception:
            _pipeline_span = None
            _token = None

        # Ensure span is ended and context detached on ALL code paths (including early returns)
        def _end_pipeline_span():
            try:
                if _pipeline_span is not None:
                    _pipeline_span.set_attribute("receipt.success", result.get("success", False))
                    _pipeline_span.set_attribute("receipt.total_ms", int((time.time() - total_start) * 1000))
                    rd = result.get("receipt_data") or {}
                    _pipeline_span.set_attribute("receipt.items_count", rd.get("total_items", 0))
                    _pipeline_span.end()
                if _token is not None:
                    from opentelemetry import trace as _otrace2
                    _otrace2.context_api.detach(_token)
            except Exception:
                pass

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
                # Preserve the is_structured flag that was cached alongside the result
                result["metadata"]["_cached_is_structured"] = _early_cache.get_meta(_early_cache_key, "is_structured", False)
        except Exception as _e:
            logger.debug(f"[Step 0/5] Early cache check failed: {_e}")

        # ─── Step 1: Save uploaded image ─────────────────────────────────
        try:
            if not result["metadata"].get("image_path"):
                logger.debug("[Step 1/5] Saving uploaded image...")
                saved_path = self._save_uploaded_image(image_path)
                result["metadata"]["image_path"] = saved_path
                logger.debug(f"[Step 1/5] Image saved to: {saved_path}")
            else:
                saved_path = result["metadata"]["image_path"]
                logger.debug("[Step 1/5] Image already saved by early cache check")
        except Exception as e:
            if not result["metadata"].get("image_path"):  # not already saved by Step 0
                result["errors"].append(f"Image save failed: {e}")
                logger.error(f"[Step 1/5] Image save failed: {e}", exc_info=True)
                _end_pipeline_span()
                return result

        # ─── Step 2: Preprocess image (skipped on early cache hit) ───────
        # Defaults in case preprocessing is skipped (cache hit path)
        preprocess_ms = 0
        preprocess_meta = None
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
                with optional_span(_tracer, "preprocess_image") as _pp_span:
                    processed_image, preprocess_meta = self.preprocessor.preprocess(saved_path)
                    preprocess_ms = int((time.time() - step_start) * 1000)
                    _pp_span.set_attribute("preprocess.duration_ms", preprocess_ms)
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
                _end_pipeline_span()
                return result

        # ─── Step 3: OCR text extraction (Hybrid Engine) ────────────────
        try:
            step_start = time.time()

            # ── Short-circuit: use cached OCR result if Step 0 hit the cache ──
            if result["metadata"].get("_cached_hybrid_result"):
                hybrid_result = result["metadata"].pop("_cached_hybrid_result")
                # Use the is_structured flag that was cached alongside the result,
                # NOT from hybrid metadata (which never stores it)
                is_structured = result["metadata"].pop("_cached_is_structured", False)
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
                # Also pass quality info from preprocessing for dynamic OCR tuning
                _quality_info = preprocess_meta.get("quality") if preprocess_meta else None
                with optional_span(_tracer, "hybrid_ocr_engine", {"ocr.mode": self.hybrid_engine.mode}) as _ocr_span:
                    hybrid_result = self.hybrid_engine.process_image(
                        image_path=saved_path,
                        processed_image=processed_image,
                        is_structured=is_structured,
                        original_color=_color_img,
                        quality_info=_quality_info,
                    )
                    _ocr_span.set_attribute("ocr.engine_used", hybrid_result.get("engine_used", "unknown"))
                    _ocr_span.set_attribute("ocr.detections", len(hybrid_result.get("ocr_detections", [])))
                    _ocr_span.set_attribute("ocr.time_ms", hybrid_result.get("ocr_time_ms", 0))

                # Store is_structured in image cache so cache hits use correct parse mode
                try:
                    from app.ocr.image_cache import get_image_cache
                    _cache = get_image_cache()
                    _hash = _cache.compute_hash(saved_path)
                    _cache.put_meta(_hash, "is_structured", is_structured)
                except Exception:
                    pass

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
                _end_pipeline_span()
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
            add_breadcrumb(
                f"OCR complete: {len(ocr_results)} detections via {engine_used}",
                category="ocr",
                engine=engine_used,
                detections=len(ocr_results),
                avg_confidence=hybrid_result["confidence_avg"],
            )

        except Exception as e:
            result["errors"].append(f"OCR extraction failed: {e}")
            logger.error(f"OCR extraction failed: {e}", exc_info=True)
            capture_exception(e, stage="ocr_extraction", image_path=str(image_path),
                             engine_mode=getattr(self.hybrid_engine, 'mode', 'unknown'))
            _end_pipeline_span()
            return result

        # ─── Step 4: Parse receipt data ──────────────────────────────────
        try:
            logger.debug("[Step 4/5] Parsing receipt data...")
            step_start = time.time()

            # ── If Azure receipt model returned structured items, use them directly ──
            with optional_span(_tracer, "parse_receipt") as _parse_span:
                if azure_structured and azure_structured.get("items"):
                    receipt_data = self._parse_azure_structured(
                        azure_structured, ocr_results, is_structured
                    )
                    _parse_span.set_attribute("parse.source", "azure_structured")
                    logger.info(
                        f"[Step 4/5] Azure structured parse: "
                        f"{receipt_data['total_items']} items"
                    )
                else:
                    # ── Standard parse: OCR detections → parser (works for both Azure Read & EasyOCR) ──
                    receipt_data = self.parser.parse(ocr_results, is_structured=is_structured)
                    _parse_span.set_attribute("parse.source", "ocr_detections")

                parse_ms = int((time.time() - step_start) * 1000)
                _parse_span.set_attribute("parse.duration_ms", parse_ms)
                _parse_span.set_attribute("parse.items_found", receipt_data.get("total_items", 0))
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
                _end_pipeline_span()
                return result

        except Exception as e:
            result["errors"].append(f"Data parsing failed: {e}")
            logger.error(f"[Step 4/5] Data parsing failed: {e}", exc_info=True)
            _end_pipeline_span()
            return result

        # ─── Steps 4b-4f: Post-OCR verification (parallelized) ──────────
        # These steps are all non-fatal and mostly independent. Running them
        # concurrently via ThreadPoolExecutor saves ~1-3s compared to serial.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        catalog_full = {}
        validation_result = None
        quality_result = None
        _image_hash = ""
        _content_fingerprint = ""
        _dedup_result = None

        # Pre-fetch catalog (needed by 4b, 4c, 4d)
        try:
            catalog_full = product_service.get_product_catalog_full()
        except Exception as e:
            logger.warning(f"[Step 4] Catalog fetch failed: {e}")

        # ── Enrich items with catalog prices BEFORE launching parallel tasks ──
        # Steps 4b, 4c, and 4f all read receipt_data["items"] concurrently.
        # If 4c mutates items (setting unit_price/line_total) while 4b reads
        # them, the total verification result becomes non-deterministic.
        # Doing the enrichment here serializes the mutation safely.
        if catalog_full:
            for item in receipt_data.get("items", []):
                code = item.get("code", "")
                if code in catalog_full:
                    cat_price = catalog_full[code].get("unit_price", 0)
                    if item.get("unit_price", 0) == 0 and cat_price > 0:
                        item["unit_price"] = cat_price
                        item["line_total"] = round(item.get("quantity", 0) * cat_price, 2)
                        item["price_source"] = "catalog"
                    elif item.get("unit_price", 0) > 0:
                        item["price_source"] = "ocr"

        def _step_4b_total_verification():
            """Step 4b: Bill Total Verification"""
            try:
                verifier = get_total_verifier()
                vr = verifier.verify(
                    ocr_detections=ocr_results,
                    parsed_items=receipt_data.get("items", []),
                    azure_structured=azure_structured,
                )
                parser_verification = receipt_data.get("total_verification", {})
                if parser_verification.get("total_qty_ocr") is not None and vr.get("ocr_total") is None:
                    parser_ocr_total = parser_verification["total_qty_ocr"]
                    vr["ocr_total"] = parser_ocr_total
                    vr["total_line_text"] = parser_verification.get("total_line_text")
                    vr["total_line_confidence"] = parser_verification.get("total_line_confidence")
                    computed = vr.get("computed_total", 0)
                    if computed and abs(parser_ocr_total - computed) < 0.01:
                        vr["total_qty_match"] = True
                        vr["verified"] = True
                        vr["confidence"] = parser_verification.get("total_line_confidence", 0.9)
                        vr["verification_method"] = "parser_exact_match"
                        vr["discrepancy"] = 0.0
                    else:
                        vr["total_qty_match"] = False
                        vr["verified"] = False
                        vr["discrepancy"] = abs(parser_ocr_total - computed) if computed else None
                        vr["verification_method"] = "parser_total_mismatch"
                vr["total_qty_ocr"] = vr.get("ocr_total")
                vr["total_qty_computed"] = vr.get("computed_total")
                vr["verification_status"] = (
                    "verified" if vr.get("verified") else
                    ("mismatch" if vr.get("ocr_total") is not None else "not_found")
                )
                return ("4b", vr)
            except Exception as e:
                logger.warning(f"[Step 4b] Total verification failed (non-fatal): {e}")
                return ("4b", None)

        def _step_4c_math_verification():
            """Step 4c: Math / Price Verification"""
            try:
                parsed_items = receipt_data.get("items", [])
                # NOTE: catalog price enrichment is done BEFORE parallel launch
                # (above the ThreadPoolExecutor block) to avoid data races.
                parser_math = receipt_data.get("math_verification", {})
                ocr_grand_total = parser_math.get("ocr_grand_total")

                # When Azure receipt model is used, it provides the monetary
                # total directly — use it if the parser didn't find one.
                if ocr_grand_total is None and azure_structured:
                    azure_total = azure_structured.get("total") or azure_structured.get("subtotal")
                    if azure_total is not None:
                        try:
                            ocr_grand_total = float(
                                str(azure_total).replace("$", "").replace("€", "")
                                .replace("£", "").replace("₹", "").replace(",", "").strip()
                            )
                            logger.debug(f"[Step 4c] Using Azure receipt total as grand total: {ocr_grand_total}")
                        except (ValueError, TypeError):
                            pass

                verifier = get_total_verifier()
                math_result = verifier.verify_math(
                    parsed_items=parsed_items,
                    catalog=catalog_full,
                    ocr_grand_total=ocr_grand_total,
                )
                if ocr_grand_total is not None and math_result.get("has_prices"):
                    math_result["grand_total_text"] = parser_math.get("grand_total_text") or (
                        f"Azure receipt model total: {ocr_grand_total}" if azure_structured else None
                    )
                    math_result["grand_total_confidence"] = parser_math.get("grand_total_confidence") or 0.95
                return ("4c", math_result)
            except Exception as e:
                logger.warning(f"[Step 4c] Math verification failed (non-fatal): {e}")
                return ("4c", None)

        def _step_4f_dedup():
            """Step 4f: Compute Dedup Hashes"""
            try:
                img_hash = dedup_service.compute_image_hash(saved_path)
                content_fp = dedup_service.compute_content_fingerprint(
                    receipt_data.get("items", [])
                )
                dedup_res = dedup_service.check_duplicate(
                    img_hash, content_fp, self.db
                )
                return ("4f", img_hash, content_fp, dedup_res)
            except Exception as e:
                logger.warning(f"[Step 4f] Dedup check failed (non-fatal): {e}")
                return ("4f", "", "", None)

        # Launch independent tasks in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(_step_4b_total_verification),
                executor.submit(_step_4c_math_verification),
                executor.submit(_step_4f_dedup),
            ]
            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res[0] == "4b" and res[1] is not None:
                        # Merge: keep existing Azure-path total_verification fields,
                        # let Step 4b fill in gaps (not overwrite).
                        existing_tv = receipt_data.get("total_verification", {})
                        if existing_tv:
                            # Step 4b's verifier may refine computed_total and
                            # verification status.  Merge carefully:
                            for k, v in res[1].items():
                                if existing_tv.get(k) is None or k in (
                                    "computed_total", "total_qty_computed",
                                    "verification_status", "total_qty_match",
                                ):
                                    existing_tv[k] = v
                            receipt_data["total_verification"] = existing_tv
                        else:
                            receipt_data["total_verification"] = res[1]
                        result["metadata"]["total_verification"] = receipt_data["total_verification"]
                        logger.info(
                            f"[Step 4b] Total verification: "
                            f"ocr_total={res[1].get('ocr_total')}, "
                            f"match={res[1].get('total_qty_match')}"
                        )
                    elif res[0] == "4c" and res[1] is not None:
                        receipt_data["math_verification"] = res[1]
                        result["metadata"]["math_verification"] = res[1]
                        logger.info(
                            f"[Step 4c] Math verification: "
                            f"has_prices={res[1].get('has_prices')}, "
                            f"all_line_ok={res[1].get('all_line_math_ok')}"
                        )
                    elif res[0] == "4f":
                        _image_hash = res[1]
                        _content_fingerprint = res[2]
                        _dedup_result = res[3]
                        if _dedup_result:
                            receipt_data["duplicate_warning"] = _dedup_result
                            result["metadata"]["duplicate_warning"] = _dedup_result
                except Exception as e:
                    logger.warning(f"[Step 4] Parallel task failed: {e}")

        # ─── Step 4d: Smart Validation (depends on 4c catalog prices) ────
        try:
            historical_stats = receipt_validator.get_historical_stats(self.db)
            validation_result = receipt_validator.validate(
                items=receipt_data.get("items", []),
                catalog=catalog_full or None,
                historical_stats=historical_stats or None,
            )
            receipt_data["validation"] = validation_result
            result["metadata"]["validation"] = validation_result.get("summary", {})
            logger.info(
                f"[Step 4d] Validation: valid={validation_result['valid']}, "
                f"warnings={validation_result['summary']['total_warnings']}, "
                f"corrections={validation_result['summary']['auto_corrections']}"
            )
        except Exception as e:
            logger.warning(f"[Step 4d] Validation failed (non-fatal): {e}")

        # ─── Step 4e: Quality Score (depends on 4b, 4c) ─────────────────
        try:
            quality_result = quality_scorer.score(
                items=receipt_data.get("items", []),
                metadata=result.get("metadata", {}),
                total_verification=receipt_data.get("total_verification"),
                math_verification=receipt_data.get("math_verification"),
            )
            receipt_data["quality"] = quality_result
            result["metadata"]["quality_score"] = quality_result["score"]
            result["metadata"]["quality_grade"] = quality_result["grade"]
            logger.info(
                f"[Step 4e] Quality: score={quality_result['score']}, "
                f"grade={quality_result['grade']}"
            )
        except Exception as e:
            logger.warning(f"[Step 4e] Quality scoring failed (non-fatal): {e}")

        # ─── Step 5: Save to database ────────────────────────────────────
        try:
            logger.debug("[Step 5/5] Saving to database...")
            with optional_span(_tracer, "database_save") as _db_span:
                receipt_id = self.db.create_receipt(
                    receipt_number=receipt_data["receipt_id"],
                    image_path=saved_path,
                    processed_image_path=processed_path,
                )

            # Add product unit info to items (reuse catalog_full from Step 4c if available)
            if not catalog_full:
                catalog_full = product_service.get_product_catalog_full()
            for item in receipt_data["items"]:
                code = item["code"]
                if code in catalog_full:
                    item["unit"] = catalog_full[code].get("unit", "Piece")

            self.db.add_receipt_items(receipt_id, receipt_data["items"])

            # Populate DB-assigned IDs back into items so the frontend
            # can PUT (update) instead of POST (duplicate) on "Confirm & Save"
            saved_receipt = self.db.get_receipt(receipt_id)
            if saved_receipt and saved_receipt.get("items"):
                for item, saved_item in zip(receipt_data["items"], saved_receipt["items"], strict=False):
                    item["id"] = saved_item["id"]

            # ── Save smart OCR metadata ──
            try:
                meta_kwargs = {}
                if _image_hash:
                    meta_kwargs["image_hash"] = _image_hash
                if _content_fingerprint:
                    meta_kwargs["content_fingerprint"] = _content_fingerprint
                # Date extracted from OCR text
                if receipt_data.get("receipt_date"):
                    meta_kwargs["receipt_date"] = receipt_data["receipt_date"]
                # Store name extracted from OCR text
                if receipt_data.get("store_name"):
                    meta_kwargs["store_name"] = receipt_data["store_name"]
                # Quality score
                if quality_result:
                    meta_kwargs["quality_score"] = int(quality_result["score"])
                    meta_kwargs["quality_grade"] = quality_result["grade"]
                if meta_kwargs:
                    self.db.update_receipt_metadata(receipt_id, **meta_kwargs)
            except Exception as e:
                logger.debug(f"Smart OCR metadata save failed (non-fatal): {e}")

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

        # ─── Record Prometheus metrics ──────────────────────────────────────────
        try:
            elapsed = time.time() - total_start
            strategy = result.get("metadata", {}).get("ocr_strategy", "unknown")
            rd = result.get("receipt_data") or {}
            _record_scan(
                strategy=strategy,
                success=result["success"],
                duration=elapsed,
                items_count=rd.get("total_items", 0),
                avg_confidence=rd.get("avg_confidence", 0),
            )
        except Exception:
            pass

        # ── End pipeline tracing span ──
        _end_pipeline_span()

        return result

    def get_receipt(self, receipt_id: int) -> dict | None:
        """Get a receipt by ID with all items."""
        return self.db.get_receipt(receipt_id)

    def get_recent_receipts(self, limit: int = 10, offset: int = 0) -> list[dict]:
        """Get the most recent receipts (paginated)."""
        return self.db.get_recent_receipts(limit, offset)

    def count_receipts(self) -> int:
        """Return total receipt count."""
        return self.db.count_receipts()

    def get_receipts_by_date(self, date: str) -> list[dict]:
        """Get all receipts for a specific date."""
        return self.db.get_receipts_by_date(date)

    def update_receipt_item(
        self,
        item_id: int,
        product_code: str,
        product_name: str,
        quantity: float,
        unit_price: float = 0.0,
        line_total: float = 0.0,
    ) -> bool:
        """Update a receipt item (manual correction). Returns False if not found.

        Also records the correction in the OCR feedback loop so future
        parsing automatically fixes the same OCR misread.
        """
        # Capture original values before update for correction recording
        try:
            original = self.db.get_receipt_item(item_id)
        except Exception:
            original = None

        success = self.db.update_receipt_item(
            item_id, product_code, product_name, quantity,
            unit_price=unit_price, line_total=line_total,
        )

        # Record correction for feedback loop (non-blocking)
        if success and original:
            try:
                correction_service.record_correction(
                    db_instance=self.db,
                    receipt_id=original.get("receipt_id", 0),
                    item_id=item_id,
                    original_code=original.get("product_code", ""),
                    corrected_code=product_code,
                    original_qty=original.get("quantity", 0),
                    corrected_qty=quantity,
                    raw_ocr_text="",
                )
            except Exception as e:
                logger.debug(f"Correction recording failed (non-fatal): {e}")

        return success

    def delete_receipt(self, receipt_id: int) -> bool:
        """Delete a receipt."""
        return self.db.delete_receipt(receipt_id)

    def delete_receipt_item(self, item_id: int) -> bool:
        """Delete a single receipt item."""
        return self.db.delete_receipt_item(item_id)

    def add_receipt_item(
        self,
        receipt_id: int,
        product_code: str,
        product_name: str,
        quantity: float,
        unit_price: float = 0.0,
        line_total: float = 0.0,
    ) -> int:
        """Add a new item to an existing receipt (for manually added rows)."""
        return self.db.add_receipt_item(
            receipt_id, product_code, product_name, quantity,
            unit_price=unit_price, line_total=line_total,
        )

    def _parse_azure_structured(self, azure_data: dict, ocr_detections: list[dict], is_structured: bool = False) -> dict:
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
        from datetime import datetime
        from difflib import get_close_matches

        items = []
        azure_items = azure_data.get("items", [])
        catalog = self.parser.product_catalog
        catalog_names = {v.upper(): k for k, v in catalog.items()}  # reverse: name → code

        def _safe_price(val) -> float:
            """Coerce Azure price/qty values to float (may be string, None, or dict)."""
            if val is None:
                return 0.0
            if isinstance(val, (int, float)):
                return float(val)
            try:
                cleaned = str(val).replace("$", "").replace("€", "").replace("£", "").replace("₹", "").replace(",", "").strip()
                return float(cleaned)
            except (ValueError, TypeError):
                return 0.0

        for azure_item in azure_items:
            description = azure_item.get("description", "").strip()
            quantity = _safe_price(azure_item.get("quantity", 1.0)) or 1.0
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
                    # Use adaptive cutoff (same as parser) to prevent false matches
                    from app.config import get_adaptive_fuzzy_cutoff
                    fuzzy_cutoff = get_adaptive_fuzzy_cutoff(len(desc_upper))
                    close = get_close_matches(
                        desc_upper, catalog_names.keys(), n=1, cutoff=fuzzy_cutoff
                    )
                    if close:
                        code = catalog_names[close[0]]
                        product_name = catalog[code]
                        match_type = "azure-fuzzy"
                    else:
                        # 4. Use description as-is (unknown product)
                        code = desc_upper[:6] if len(desc_upper) >= 2 else "UNK"
                        match_type = "azure-unmatched"

            # Look up unit and price from catalog
            unit = "Piece"
            unit_price = _safe_price(azure_item.get("unit_price")) or _safe_price(azure_item.get("price"))
            line_total = _safe_price(azure_item.get("total_price")) or _safe_price(azure_item.get("amount"))
            if code and code in catalog:
                product_info = product_service.get_product(code)
                if product_info:
                    unit = product_info.get("unit", "Piece")
                    # Use catalog price if Azure didn't provide one
                    if unit_price == 0:
                        unit_price = _safe_price(product_info.get("unit_price"))

            # Compute line_total if we have rate but no total
            final_qty = max(1.0, min(9999.0, quantity))
            if line_total == 0 and unit_price > 0:
                line_total = round(final_qty * unit_price, 2)

            items.append({
                "code": code,
                "product": product_name,
                "quantity": final_qty,
                "unit": unit,
                "confidence": round(confidence, 4),
                "needs_review": match_type == "azure-unmatched" or confidence < 0.6,
                "match_type": match_type,
                "raw_text": description,
                "y_center": 0,
                "unit_price": unit_price,
                "line_total": line_total,
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
            # Use parser's date/store as fallback if Azure didn't provide them
            if not receipt_date:
                receipt_date = parsed.get("receipt_date")
            if not store_name:
                store_name = parsed.get("store_name")

        # Calculate stats
        avg_confidence = sum(i["confidence"] for i in items) / len(items) if items else 0
        needs_review = any(i["needs_review"] for i in items) or avg_confidence < 0.85

        receipt_number = self.parser._generate_receipt_number()

        # ── Extract receipt_date and store_name ──
        # Azure's receipt model provides these directly; fall back to OCR text scan.
        receipt_date = azure_data.get("transaction_date")
        store_name = azure_data.get("merchant")
        if not receipt_date or not store_name:
            try:
                grouped = self.parser._group_into_lines(ocr_detections, is_structured=is_structured)
                if not receipt_date:
                    receipt_date = self.parser._extract_receipt_date(grouped)
                if not store_name:
                    store_name = self.parser._extract_store_name(grouped)
            except Exception as e:
                logger.debug(f"[Azure Parse] Date/store extraction failed: {e}")

        # Bill total from Azure structured data
        azure_total = azure_data.get("total") or azure_data.get("subtotal")
        computed_qty_total = round(sum(it.get("quantity", 0) for it in items), 1)
        computed_monetary_total = round(sum(it.get("line_total", 0) for it in items), 2)

        # Safely convert azure_total to float (may be string like "$10.50" or "N/A")
        azure_total_float = None
        if azure_total is not None:
            try:
                # Strip currency symbols and whitespace before converting
                cleaned = str(azure_total).replace("$", "").replace("€", "").replace("£", "").replace("₹", "").replace(",", "").strip()
                azure_total_float = float(cleaned)
            except (ValueError, TypeError):
                logger.warning("Azure total not numeric: %r", azure_total)

        # ── Extract grand total from raw OCR text ──
        # The regular parser does this in its pre-scan, but this Azure structured
        # path bypasses the parser entirely.  Scan the raw OCR detections for
        # "Grand Total 17450"-style text so verify_math() can compare it.
        ocr_grand_total = None
        grand_total_text_found = None
        grand_total_confidence = None
        gt_keyword_re = self.parser._GRAND_TOTAL_KEYWORD_RE
        gt_patterns = self.parser.GRAND_TOTAL_PATTERNS

        for det in ocr_detections:
            text = det.get("text", "")
            conf = det.get("confidence", 0.0)
            if gt_keyword_re.search(text):
                for pattern in gt_patterns:
                    m = pattern.search(text)
                    if m:
                        try:
                            val = float(m.group(1))
                            if val > 0:
                                ocr_grand_total = val
                                grand_total_text_found = text
                                grand_total_confidence = conf
                                logger.info(f"[Azure Parse] Grand total from OCR text: {text!r} → {val}")
                        except (ValueError, TypeError):
                            pass
                        break

        # Fall back to Azure receipt model's structured total
        if ocr_grand_total is None and azure_total_float is not None:
            ocr_grand_total = azure_total_float
            grand_total_text_found = f"Azure receipt model total: {azure_total_float}"
            grand_total_confidence = 0.95
            logger.info(f"[Azure Parse] Grand total from Azure structured: {azure_total_float}")

        # Build math_verification so Step 4c can use it
        has_prices = any(it.get("unit_price", 0) > 0 for it in items)
        math_verification = {
            "has_prices": has_prices,
            "ocr_grand_total": ocr_grand_total,
            "grand_total_text": grand_total_text_found,
            "grand_total_confidence": grand_total_confidence,
        }

        # ── Scan OCR text for "Total Qty" lines ──
        # The regular parser does this in its pre-scan but the Azure structured
        # path bypasses the parser.  Scan raw detections so Bill Total Verified
        # panel can show the quantity total.
        total_qty_ocr = None
        total_qty_text = None
        total_qty_conf = None
        try:
            grouped_for_totals = self.parser._group_into_lines(ocr_detections, is_structured=is_structured)
            for line_info in grouped_for_totals:
                raw_text = line_info["text"]
                # Skip grand total lines (monetary, not qty)
                if self.parser._GRAND_TOTAL_KEYWORD_RE.search(raw_text):
                    continue
                if self.parser._is_total_line(raw_text):
                    val, txt = self.parser._extract_total_from_line(raw_text)
                    if val is not None:
                        total_qty_ocr = val
                        total_qty_text = txt
                        total_qty_conf = line_info["confidence"]
                        logger.info(f"[Azure Parse] Total Qty from OCR text: {txt!r} → {val}")
                        break
        except Exception as e:
            logger.debug(f"[Azure Parse] Total Qty scan failed: {e}")

        # Azure "total" is a monetary value — compare against monetary sum.
        # Quantity sum is tracked separately for qty-based verification.
        total_verification = {
            "total_qty_ocr": total_qty_ocr,
            "total_qty_computed": computed_qty_total,
            "total_line_text": total_qty_text or (f"Azure receipt model total: {azure_total}" if azure_total else None),
            "total_line_confidence": total_qty_conf or (0.95 if azure_total else None),
            "total_qty_match": None,
            "verification_status": "not_found",
            "ocr_total": azure_total_float,
            "computed_total": computed_monetary_total if computed_monetary_total > 0 else None,
        }
        # Qty-based verification (if we found a "Total Qty" line)
        if total_qty_ocr is not None:
            total_verification["total_qty_match"] = abs(total_qty_ocr - computed_qty_total) < 0.01
            total_verification["verification_status"] = (
                "verified" if total_verification["total_qty_match"] else "mismatch"
            )
        elif azure_total_float is not None and computed_monetary_total > 0:
            # Compare monetary totals
            total_verification["total_qty_match"] = abs(azure_total_float - computed_monetary_total) < 0.01
            total_verification["verification_status"] = (
                "verified" if total_verification["total_qty_match"] else "mismatch"
            )
        elif azure_total_float is not None:
            # No price data — fall back to qty comparison (may not match monetary total)
            total_verification["verification_status"] = "mismatch"
            total_verification["total_qty_match"] = False

        return {
            "receipt_id": receipt_number,
            "scan_timestamp": datetime.now().isoformat(),
            "items": items,
            "total_items": len(items),
            "avg_confidence": round(avg_confidence, 4),
            "needs_review": needs_review,
            "unparsed_lines": [],
            "processing_status": "success" if items else "no_items_found",
            "total_verification": total_verification,
            "math_verification": math_verification,
            "receipt_date": receipt_date,
            "store_name": store_name,
        }

    def _quick_item_count(self, ocr_results: list[dict]) -> int:
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

        # Use hard-link (instant, zero-copy) when on the same filesystem.
        # Falls back to shutil.copy2 only for cross-device or permission errors.
        try:
            os.link(str(src), str(dest))
        except (OSError, NotImplementedError):
            shutil.copy2(str(src), str(dest))
        logger.debug(f"Image saved to: {dest}")
        return str(dest)


# Singleton
receipt_service = ReceiptService()
