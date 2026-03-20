"""
Hybrid OCR Engine Orchestrator.

Intelligently routes receipt images through the best OCR pipeline based on:
    1. Availability: Azure cloud vs local EasyOCR
    2. Receipt type: Structured (boxed) vs handwritten
    3. Quality: Uses Azure receipt model first, falls back to Read model,
       then local EasyOCR as last resort
    4. Cross-verification: Optional dual-engine validation for critical accuracy

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                     HybridOCREngine                             │
    │                                                                  │
    │  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐  │
    │  │ Azure Doc    │────►│ Azure Read   │────►│ Local EasyOCR    │  │
    │  │ Intelligence │     │ Model        │     │ (fallback)       │  │
    │  │ (Receipt)    │     │ (handwriting)│     │                  │  │
    │  └─────────────┘     └──────────────┘     └──────────────────┘  │
    │         │                    │                      │            │
    │         └────────────────────┴──────────────────────┘            │
    │                         ▼                                        │
    │              ┌─────────────────────┐                             │
    │              │  Result Merger &    │                             │
    │              │  Quality Scorer     │                             │
    │              └─────────────────────┘                             │
    └──────────────────────────────────────────────────────────────────┘

Engine Modes:
    - "auto":  Azure primary → EasyOCR fallback (RECOMMENDED)
    - "azure": Azure only (fails if unavailable)
    - "local": EasyOCR only (offline, original behavior)
"""

import contextlib
import logging
import re
import threading
import time

from app.tracing import get_tracer, optional_span

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


class HybridOCREngine:
    """
    Orchestrator that selects and runs the best OCR strategy per image.

    Produces output in a unified format regardless of which engine(s) ran,
    so downstream parser and services work unchanged.

    Cost-optimization features:
        - Usage tracking with daily/monthly hard limits
        - Image hash caching (no duplicate Azure calls)
        - Smart routing: skip Azure if local confidence is high enough
        - Image compression before Azure upload (3-5× smaller)
        - Single-model selection (receipt OR read, not both)
    """

    def __init__(self):
        from app.config import OCR_ENGINE_MODE
        self.mode = OCR_ENGINE_MODE.lower().strip()
        self._azure_engine = None
        self._local_engine = None
        self._local_engine_2 = None   # Second reader for thread-safe parallel dual-pass
        self._engine2_lock = threading.Lock()  # Guards lazy init of _local_engine_2
        self._azure_checked = False
        self._azure_ok = False

        # Cost-control components (lazy-loaded)
        self._usage_tracker = None
        self._image_cache = None

        logger.info(f"HybridOCREngine initialized (mode={self.mode})")

    # ─── Lazy engine accessors ────────────────────────────────────────────

    @property
    def azure_engine(self):
        """Lazy-load Azure engine on first use."""
        if self._azure_engine is not None:
            return self._azure_engine

        if not self._azure_checked:
            self._azure_checked = True
            try:
                from app.ocr.azure_engine import get_azure_engine
                self._azure_engine = get_azure_engine()
                self._azure_ok = self._azure_engine is not None
            except Exception as e:
                logger.warning(f"Azure engine init failed: {e}")
                self._azure_ok = False

        return self._azure_engine

    @property
    def local_engine(self):
        """Lazy-load local EasyOCR engine on first use."""
        if self._local_engine is None:
            from app.ocr.engine import get_ocr_engine
            self._local_engine = get_ocr_engine()
        return self._local_engine

    @property
    def local_engine_2(self):
        """Lazy-load a second independent OCREngine for thread-safe parallel dual-pass.

        Using the same reader instance from two threads is unsafe (shared
        internal tensors / buffers).  A separate instance gives each thread
        its own PyTorch graph, enabling true concurrent execution while
        PyTorch releases the GIL during neural-network forward passes.
        Only materialised when OCR_PARALLEL_DUAL_PASS=True.
        """
        if self._local_engine_2 is not None:
            return self._local_engine_2
        with self._engine2_lock:
            if self._local_engine_2 is None:
                from app.config import OCR_LANGUAGE, OCR_USE_GPU
                from app.ocr.engine import OCREngine
                logger.info("[HybridOCR] Initializing second OCR reader for parallel dual-pass...")
                self._local_engine_2 = OCREngine(language=OCR_LANGUAGE, use_gpu=OCR_USE_GPU)
        return self._local_engine_2

    @property
    def usage_tracker(self):
        """Lazy-load usage tracker."""
        if self._usage_tracker is None:
            from app.ocr.usage_tracker import get_usage_tracker
            self._usage_tracker = get_usage_tracker()
        return self._usage_tracker

    @property
    def image_cache(self):
        """Lazy-load image cache."""
        if self._image_cache is None:
            from app.ocr.image_cache import get_image_cache
            self._image_cache = get_image_cache()
        return self._image_cache

    # ─── Public API ──────────────────────────────────────────────────────

    def process_image(
        self,
        image_path: str,
        processed_image=None,
        is_structured: bool = False,
        original_color=None,
        quality_info: dict = None,
    ) -> dict:
        """
        Process a receipt image through the optimal OCR pipeline.

        This is the MAIN entry point. It replaces the multi-pass OCR logic
        that was previously in receipt_service.py.

        Args:
            image_path: Path to the original uploaded image file.
            processed_image: Preprocessed grayscale image (numpy array) for local OCR.
            is_structured: Whether a grid/table structure was detected.
            original_color: Pre-loaded color image (BGR numpy array) to avoid
                            re-reading from disk. If None, loaded from image_path.
            quality_info: Image quality assessment dict from preprocessor
                          (score, is_blurry, is_low_contrast, is_too_dark, etc.)

        Returns:
            Unified result dict:
            {
                "engine_used": "azure-receipt" | "azure-read" | "local" | "hybrid",
                "ocr_detections": [...],  # EasyOCR-compatible format
                "azure_structured": {...} | None,  # Azure receipt model data
                "ocr_time_ms": int,
                "ocr_passes": int,
                "confidence_avg": float,
                "metadata": {...},
            }
        """

        with optional_span(_tracer, "hybrid_engine.route", {"ocr.mode": self.mode}) as span:
            if self.mode == "local":
                result = self._run_local_pipeline(processed_image, image_path, is_structured, original_color=original_color, quality_info=quality_info)
            elif self.mode == "azure":
                result = self._run_azure_pipeline(image_path, processed_image, is_structured)
            else:  # "auto" — recommended
                result = self._run_auto_pipeline(image_path, processed_image, is_structured, original_color=original_color, quality_info=quality_info)
            span.set_attribute("ocr.engine_used", result.get("engine_used", "unknown"))
            span.set_attribute("ocr.detections", len(result.get("ocr_detections", [])))
            return result

    def _run_auto_pipeline(
        self,
        image_path: str,
        processed_image,
        is_structured: bool,
        original_color=None,
        quality_info: dict = None,
    ) -> dict:
        """
        AUTO mode: Smart routing with MAXIMUM cost efficiency.

        Strategy (designed to minimize Azure page consumption):
        1. Check image cache → return cached result if available (FREE)
        2. Run image quality gate → reject blurry/dark images before Azure (saves wasted pages)
        3. Run local OCR first → if confidence + detection count are good enough, SKIP Azure
        4. Check usage limits → if exhausted, return local result
        5. Call Azure with SINGLE model (read-only by default — cheapest + best for handwriting)
        6. Cache Azure result for 24 hours (prevents duplicate charges)
        7. Fall back to local if Azure fails

        Page budget math (free tier):
            500 pages/month ÷ ~22 work days = ~22 scans/day
            With local-first gating at 0.72 confidence, ~40-60% of scans skip Azure
            Effective capacity: ~35-50 scans/day = ~800-1100 scans/month
        """
        from app.config import (
            AZURE_MODEL_STRATEGY,
            AZURE_RECEIPT_MIN_ITEMS,
            HYBRID_CROSS_VERIFY,
            IMAGE_QUALITY_GATE_ENABLED,
            LOCAL_CATALOG_MATCH_SKIP_THRESHOLD,
            LOCAL_CONFIDENCE_SKIP_THRESHOLD,
            LOCAL_MIN_DETECTIONS_SKIP,
        )

        total_start = time.time()
        metadata = {"strategy": "auto", "attempts": [], "azure_pages_used": 0}

        # ── Step 0: Check image cache (FREE — prevents duplicate Azure bills) ──
        cache_key = None
        try:
            cache_key = self.image_cache.compute_hash(image_path)
            cached_result = self.image_cache.get(cache_key)
            if cached_result is not None:
                import copy
                result_copy = copy.deepcopy(cached_result)
                total_ms = int((time.time() - total_start) * 1000)
                result_copy["ocr_time_ms"] = total_ms
                result_copy["metadata"] = {"strategy": "auto-cached", "cache": "hit", "azure_pages_used": 0}
                logger.info(f"[Hybrid] ✅ Cache HIT — saved 1 Azure page ({total_ms}ms)")
                return result_copy
        except Exception as e:
            logger.debug(f"[Hybrid] Cache check failed: {e}")

        # ── Step 1: Image quality gate (don't waste Azure on bad images) ──
        if IMAGE_QUALITY_GATE_ENABLED and processed_image is not None:
            try:
                quality = self._check_image_quality(processed_image)
                metadata["image_quality"] = quality

                if not quality["acceptable"]:
                    logger.warning(
                        f"[Hybrid] ⚠ Image quality too low for Azure "
                        f"(sharpness={quality['sharpness']:.1f}, brightness={quality['brightness']:.0f}). "
                        f"Using local OCR only — Azure page saved!"
                    )
                    metadata["attempts"].append({
                        "engine": "azure-skipped-quality",
                        "reason": quality["reason"],
                    })
                    # Run local and return — don't burn an Azure page on garbage
                    local_result = self._run_local_pipeline(processed_image, image_path, is_structured, original_color=original_color, quality_info=quality_info)
                    total_ms = int((time.time() - total_start) * 1000)
                    local_result["ocr_time_ms"] = total_ms
                    local_result["metadata"] = {
                        **metadata,
                        "strategy": "auto-quality-gate",
                        "quality_issue": quality["reason"],
                    }
                    return local_result
            except Exception as e:
                logger.debug(f"[Hybrid] Quality check failed: {e}")

        # ── Step 2: Smart local screening + Azure routing ─────────────
        # PERFORMANCE FIX v2: Parallel fast-screen + Azure image prep.
        #   A) When Azure is available:
        #      - Run fast single-pass screening (~3-4s) CONCURRENTLY with
        #        Azure image optimization (~30ms). Image bytes are ready
        #        the instant screening finishes → zero wait for Azure prep.
        #      - If good enough → run full local pipeline for quality
        #      - If insufficient → send pre-built bytes DIRECTLY to Azure
        #   B) When Azure is NOT available: full local pipeline as before
        #   C) SPECULATIVE PARALLEL: when enabled, fires Azure API call
        #      concurrently with fast screen (saves 1-3s but always burns a page).
        local_result = None
        _azure_is_viable = self.azure_engine is not None
        _azure_image_bytes = None  # Pre-optimized bytes for Azure (prepared in parallel)

        # ── Step 2a: Try speculative parallel if enabled ──
        from app.config import AZURE_SPECULATIVE_PARALLEL
        if AZURE_SPECULATIVE_PARALLEL and _azure_is_viable and processed_image is not None:
            try:
                spec_result = self._run_speculative_parallel(
                    image_path, processed_image, is_structured,
                    original_color=original_color, quality_info=quality_info,
                    total_start=total_start, metadata=metadata, cache_key=cache_key,
                )
                if spec_result is not None:
                    return spec_result
            except Exception as e:
                logger.warning(f"[Hybrid] Speculative parallel failed, falling back: {e}")
            # Fall through to normal flow if speculative failed

        # ── Step 2b: Normal fast-screen + Azure prep (non-speculative) ──
        if processed_image is not None:
            try:
                if _azure_is_viable:
                    # ── Parallel: fast screen + Azure image prep ──
                    # While the fast OCR screen runs (~3-4s), prepare the Azure
                    # upload bytes from the already-loaded color image in a
                    # background thread. This saves ~30-50ms of sequential I/O.
                    from concurrent.futures import ThreadPoolExecutor as _ScreenPool

                    screen_start = time.time()
                    from app.ocr.preprocessor import ImagePreprocessor
                    _cropped = ImagePreprocessor.crop_to_content_static(processed_image)

                    def _prep_azure_bytes():
                        """Pre-optimize image for Azure upload while screening runs."""
                        try:
                            # Pass the pre-loaded color image to avoid cv2.imread disk re-read
                            return self.azure_engine._optimize_image_for_upload(
                                image_path, preloaded_image=original_color,
                            )
                        except Exception as _e:
                            logger.debug(f"[Hybrid] Azure image prep failed: {_e}")
                            return None

                    with _ScreenPool(max_workers=2) as _pool:
                        _azure_prep_future = _pool.submit(_prep_azure_bytes)
                        # Fast screen runs on main thread (uses local_engine reader)
                        fast_dets = self.local_engine.extract_text_fast(_cropped)
                        # Collect Azure bytes (should already be done — ~30ms vs ~3s)
                        _azure_image_bytes = _azure_prep_future.result(timeout=5)

                    screen_ms = int((time.time() - screen_start) * 1000)

                    local_items = len(fast_dets)
                    raw_conf = self._avg_confidence(fast_dets)
                    calibrated_conf = self._calibrated_avg_confidence(fast_dets)
                    catalog_match_rate = self._catalog_match_rate(fast_dets)

                    metadata["attempts"].append({
                        "engine": "local-fast-screen",
                        "detections": local_items,
                        "confidence": round(raw_conf, 4),
                        "calibrated_confidence": round(calibrated_conf, 4),
                        "catalog_match_rate": round(catalog_match_rate, 4),
                        "screen_ms": screen_ms,
                    })

                    effective_threshold = LOCAL_CONFIDENCE_SKIP_THRESHOLD
                    if not is_structured:
                        effective_threshold = max(effective_threshold, 0.88)

                    skip_azure = (
                        calibrated_conf >= effective_threshold
                        and local_items >= LOCAL_MIN_DETECTIONS_SKIP
                        and catalog_match_rate >= LOCAL_CATALOG_MATCH_SKIP_THRESHOLD
                    )

                    if skip_azure:
                        # Good enough → run full local pipeline for best quality
                        logger.info(
                            f"[Hybrid] ✅ Fast screen GOOD in {screen_ms}ms "
                            f"(cal_conf={calibrated_conf:.3f}, raw_conf={raw_conf:.3f}, "
                            f"items={local_items}, catalog={catalog_match_rate:.1%}), "
                            f"running full local pipeline..."
                        )
                        local_result = self._run_local_pipeline(
                            processed_image, image_path, is_structured,
                            original_color=original_color, quality_info=quality_info,
                        )
                        total_ms = int((time.time() - total_start) * 1000)
                        local_result["ocr_time_ms"] = total_ms
                        local_result["metadata"] = {
                            **metadata,
                            "strategy": "auto-local-skip",
                            "azure_pages_used": 0,
                            "reason": (
                                f"Calibrated confidence {calibrated_conf:.3f} >= {effective_threshold} "
                                f"AND {local_items} detections >= {LOCAL_MIN_DETECTIONS_SKIP} "
                                f"AND catalog match {catalog_match_rate:.1%} >= {LOCAL_CATALOG_MATCH_SKIP_THRESHOLD:.0%}"
                            ),
                        }
                        logger.info(
                            f"[Hybrid] ✅ Local OCR GOOD ENOUGH "
                            f"(cal_conf={calibrated_conf:.3f}, items={local_items}), "
                            f"Azure page SAVED ({total_ms}ms)"
                        )
                        return local_result
                    else:
                        # NOT good enough → build lightweight fallback from fast screen,
                        # proceed DIRECTLY to Azure (skip expensive full multi-pass!)
                        logger.info(
                            f"[Hybrid] Fast screen INSUFFICIENT in {screen_ms}ms "
                            f"(cal_conf={calibrated_conf:.3f}, raw_conf={raw_conf:.3f}, "
                            f"items={local_items}, catalog={catalog_match_rate:.1%}), "
                            f"routing DIRECTLY to Azure (full local pipeline skipped)..."
                        )
                        local_result = {
                            "engine_used": "local",
                            "ocr_detections": fast_dets,
                            "azure_structured": None,
                            "ocr_time_ms": screen_ms,
                            "ocr_passes": 1,
                            "confidence_avg": round(raw_conf, 4),
                            "metadata": {"strategy": "local-fast-screen"},
                        }
                        # Proceed to Step 3 (Azure) with fast result as fallback

                else:
                    # ── No Azure → full local pipeline (original behavior) ──
                    local_result = self._run_local_pipeline(
                        processed_image, image_path, is_structured,
                        original_color=original_color, quality_info=quality_info,
                    )
                    # No Azure to route to, just return local result
                    total_ms = int((time.time() - total_start) * 1000)
                    local_result["ocr_time_ms"] = total_ms
                    local_result["metadata"] = {
                        **metadata,
                        "strategy": "auto-local-only",
                        "azure_pages_used": 0,
                    }
                    return local_result

            except Exception as e:
                logger.warning(f"[Hybrid] Local screening/pipeline failed: {e}")

        # ── Step 3: Check Azure usage limits before calling ─────────────
        if self.azure_engine is not None:
            try:
                usage_check = self.usage_tracker.can_call_azure()
                can_call = usage_check.get("allowed", False) if isinstance(usage_check, dict) else usage_check
                reason = usage_check.get("reason", "") if isinstance(usage_check, dict) else ""

                if not can_call:
                    logger.warning(f"[Hybrid] Azure BLOCKED by usage limit: {reason}")
                    metadata["attempts"].append({
                        "engine": "azure-blocked",
                        "reason": reason,
                    })
                    # Azure blocked → run full local pipeline for best quality
                    # (fast screening result is insufficient, so give it the full treatment)
                    local_result = self._run_local_pipeline(processed_image, image_path, is_structured, original_color=original_color, quality_info=quality_info)
                    total_ms = int((time.time() - total_start) * 1000)
                    local_result["ocr_time_ms"] = total_ms
                    local_result["metadata"] = {
                        **metadata,
                        "strategy": "auto-usage-limited",
                        "azure_pages_used": 0,
                        "reason": reason,
                    }
                    return local_result

                # Log remaining budget for visibility
                daily_remaining = usage_check.get("daily_remaining", "?") if isinstance(usage_check, dict) else "?"
                monthly_remaining = usage_check.get("monthly_remaining", "?") if isinstance(usage_check, dict) else "?"
                logger.info(
                    f"[Hybrid] Azure budget: {daily_remaining} pages left today, "
                    f"{monthly_remaining} left this month"
                )
            except Exception as e:
                logger.debug(f"[Hybrid] Usage check failed: {e}")

        # ── Step 4: Call Azure — SINGLE model only (1 page consumed) ────
        # Pass pre-optimized image bytes when available (from parallel prep
        # in Step 2) to skip the disk re-read inside Azure engine.
        if self.azure_engine is not None:
            try:
                attempt_start = time.time()

                with optional_span(_tracer, "azure_api_call", {"azure.strategy": AZURE_MODEL_STRATEGY}) as _az_span:
                    if AZURE_MODEL_STRATEGY == "receipt-only":
                        # Use receipt model (more expensive but structured output)
                        logger.info("[Hybrid] Azure: Using receipt model (receipt-only strategy)")
                        azure_result = self.azure_engine.extract_receipt_structured(
                            image_path, image_bytes=_azure_image_bytes,
                        )
                        azure_items = azure_result.get("items", [])
                        azure_detections = azure_result.get("ocr_detections", [])
                        model_used = "azure-receipt"
                        metadata["azure_pages_used"] = 1

                    elif AZURE_MODEL_STRATEGY == "receipt-then-read":
                        # Legacy: try receipt, fall back to read (CAN BURN 2 PAGES!)
                        logger.info("[Hybrid] Azure: receipt-then-read strategy (may use 2 pages!)")
                        azure_result = self.azure_engine.extract_receipt_structured(
                            image_path, image_bytes=_azure_image_bytes,
                        )
                        azure_items = azure_result.get("items", [])
                        azure_detections = azure_result.get("ocr_detections", [])
                        model_used = "azure-receipt"
                        metadata["azure_pages_used"] = 1

                        # If receipt model found too few items, try read model (2nd page!)
                        if len(azure_items) < AZURE_RECEIPT_MIN_ITEMS:
                            can_call_read = True
                            try:
                                usage_check_2 = self.usage_tracker.can_call_azure()
                                can_call_read = usage_check_2.get("allowed", False) if isinstance(usage_check_2, dict) else usage_check_2
                            except Exception:
                                pass

                            if can_call_read:
                                read_detections = self.azure_engine.extract_text_read(
                                    image_path, image_bytes=_azure_image_bytes,
                                )
                                if read_detections:
                                    azure_detections = read_detections
                                    model_used = "azure-read"
                                    metadata["azure_pages_used"] = 2
                                    logger.warning("[Hybrid] ⚠ Used 2 Azure pages (receipt+read)")

                    else:
                        # DEFAULT: read-only — cheapest, best for handwritten ($0.0015/page)
                        logger.info("[Hybrid] Azure: Using read model (read-only strategy — most efficient)")
                        read_detections = self.azure_engine.extract_text_read(
                            image_path, image_bytes=_azure_image_bytes,
                        )
                        azure_result = {"items": [], "ocr_detections": read_detections}
                        azure_items = []
                        azure_detections = read_detections
                        model_used = "azure-read"
                        metadata["azure_pages_used"] = 1

                    _az_span.set_attribute("azure.model_used", model_used)
                    _az_span.set_attribute("azure.pages_consumed", metadata["azure_pages_used"])
                    _az_span.set_attribute("azure.detections", len(azure_detections))

                attempt_ms = int((time.time() - attempt_start) * 1000)

                metadata["attempts"].append({
                    "engine": model_used,
                    "items_found": len(azure_items),
                    "detections": len(azure_detections),
                    "time_ms": attempt_ms,
                    "pages_consumed": metadata["azure_pages_used"],
                })

                # Build result
                if azure_detections or azure_items:
                    avg_conf = (
                        sum(i.get("confidence", 0) for i in azure_items) / len(azure_items)
                        if azure_items
                        else self._avg_confidence(azure_detections)
                    )
                    total_ms = int((time.time() - total_start) * 1000)

                    # Preserve azure_structured even without items if it has
                    # useful metadata (total, subtotal, merchant, transaction_date)
                    _has_useful_meta = bool(
                        azure_items
                        or azure_result.get("total")
                        or azure_result.get("subtotal")
                        or azure_result.get("merchant")
                        or azure_result.get("transaction_date")
                    )
                    result = {
                        "engine_used": model_used,
                        "ocr_detections": azure_detections,
                        "azure_structured": azure_result if _has_useful_meta else None,
                        "ocr_time_ms": total_ms,
                        "ocr_passes": 1,
                        "confidence_avg": round(avg_conf, 4),
                        "metadata": metadata,
                    }

                    # Cache the result — only if quality is high enough.
                    # Bad Azure responses (network hiccup, unreadable image) must NOT
                    # be cached: a 24h lock-in on an empty result blocks all retries.
                    _cache_worthy = (
                        len(azure_detections) >= 2
                        or (azure_items and avg_conf > 0.5)
                    )
                    if cache_key and _cache_worthy:
                        with contextlib.suppress(Exception):
                            self.image_cache.put(cache_key, result)
                    elif cache_key:
                        logger.debug(
                            "[Hybrid] Skipping cache write — result too sparse "
                            f"(detections={len(azure_detections)}, conf={avg_conf:.2f})"
                        )

                    logger.info(
                        f"[Hybrid] ✅ Azure SUCCESS: {model_used}, "
                        f"{len(azure_detections)} detections, "
                        f"{metadata['azure_pages_used']} page(s) used, {total_ms}ms"
                    )
                    return result

            except Exception as e:
                logger.warning(
                    f"[Hybrid] Azure failed ({type(e).__name__}: {e}), "
                    f"falling back to local EasyOCR..."
                )
                metadata["attempts"].append({
                    "engine": "azure-error",
                    "error": str(e),
                })

        else:
            logger.info("[Hybrid] Azure not available, using local EasyOCR")
            metadata["attempts"].append({"engine": "azure-skipped", "reason": "not configured"})

        # ── Step 5: Local EasyOCR fallback ───────────────────────────────
        # If Azure failed or wasn't available, run full local pipeline
        # for best quality (not just the fast screening result)
        if local_result is None or local_result.get("metadata", {}).get("strategy") == "local-fast-screen":
            logger.info("[Hybrid] Running full local pipeline as fallback...")
            local_result = self._run_local_pipeline(processed_image, image_path, is_structured, original_color=original_color, quality_info=quality_info)

        local_result["metadata"] = {**metadata, **local_result.get("metadata", {})}
        local_result["metadata"]["strategy"] = "auto-fallback-local"

        # ── Optional: Cross-verification (costs an extra page!) ──────────
        if HYBRID_CROSS_VERIFY and self.azure_engine is not None:
            try:
                # Check budget before burning a page for verification
                usage_check = self.usage_tracker.can_call_azure()
                can_verify = usage_check.get("allowed", False) if isinstance(usage_check, dict) else usage_check
                if can_verify:
                    logger.info("[Hybrid] Cross-verify: running Azure Read for verification...")
                    verify_detections = self.azure_engine.extract_text_read(image_path)
                    metadata["azure_pages_used"] = metadata.get("azure_pages_used", 0) + 1
                    if verify_detections:
                        local_result = self._cross_verify_results(
                            local_result, verify_detections
                        )
                else:
                    logger.info("[Hybrid] Cross-verify skipped (budget limit)")
            except Exception as e:
                logger.debug(f"[Hybrid] Cross-verify skipped (Azure error): {e}")

        return local_result

    def _run_speculative_parallel(
        self,
        image_path: str,
        processed_image,
        is_structured: bool,
        original_color=None,
        quality_info: dict = None,
        total_start: float = 0,
        metadata: dict = None,
        cache_key: str = None,
    ) -> dict | None:
        """
        Speculative parallel: fire Azure API + fast screen concurrently.

        The Azure API call (~2-5s) runs in a background thread while the fast
        local screen (~3-4s) runs on the calling thread. When the screen
        finishes first:
          - "Good enough" + Azure done → use Azure result (already paid, better quality)
          - "Good enough" + Azure not done → use full local pipeline (don't wait)
          - "Insufficient" → wait for Azure result (it's already in-flight)

        Trade-off: ALWAYS consumes an Azure page ($0.01) even when local is
        sufficient. Enable via AZURE_SPECULATIVE_PARALLEL=true when speed
        matters more than cost.

        Returns:
            Result dict if speculative path handled the request.
            None if it couldn't handle it (caller falls back to normal flow).
        """
        from concurrent.futures import ThreadPoolExecutor

        from app.config import (
            AZURE_API_TIMEOUT,
            AZURE_MODEL_STRATEGY,
            LOCAL_CATALOG_MATCH_SKIP_THRESHOLD,
            LOCAL_CONFIDENCE_SKIP_THRESHOLD,
            LOCAL_MIN_DETECTIONS_SKIP,
        )
        from app.ocr.preprocessor import ImagePreprocessor

        if metadata is None:
            metadata = {"strategy": "speculative", "attempts": [], "azure_pages_used": 0}

        _cropped = ImagePreprocessor.crop_to_content_static(processed_image)
        screen_start = time.time()

        def _azure_full_pipeline():
            """Image prep + usage check + Azure API call (runs in background thread)."""
            try:
                usage_check = self.usage_tracker.can_call_azure()
                can_call = usage_check.get("allowed", False) if isinstance(usage_check, dict) else usage_check
                if not can_call:
                    reason = usage_check.get("reason", "limit") if isinstance(usage_check, dict) else "limit"
                    return {"status": "blocked", "reason": reason}

                img_bytes = self.azure_engine._optimize_image_for_upload(
                    image_path, preloaded_image=original_color,
                )

                if AZURE_MODEL_STRATEGY == "receipt-only":
                    result = self.azure_engine.extract_receipt_structured(
                        image_path, image_bytes=img_bytes,
                    )
                    return {
                        "status": "success", "result": result,
                        "model": "azure-receipt", "pages": 1,
                    }
                else:
                    dets = self.azure_engine.extract_text_read(
                        image_path, image_bytes=img_bytes,
                    )
                    return {
                        "status": "success",
                        "result": {"items": [], "ocr_detections": dets},
                        "model": "azure-read", "pages": 1,
                    }
            except Exception as e:
                logger.warning(f"[Hybrid] Speculative Azure thread failed: {e}")
                return {"status": "error", "error": str(e)}

        # Fire both concurrently — use wait=False so we don't block on exit
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="spec-ocr")
        try:
            azure_future = pool.submit(_azure_full_pipeline)
            fast_dets = self.local_engine.extract_text_fast(_cropped)
        except Exception as e:
            pool.shutdown(wait=False)
            logger.warning(f"[Hybrid] Speculative parallel startup failed: {e}")
            return None

        screen_ms = int((time.time() - screen_start) * 1000)

        # Compute screening metrics
        local_items = len(fast_dets)
        raw_conf = self._avg_confidence(fast_dets)
        calibrated_conf = self._calibrated_avg_confidence(fast_dets)
        catalog_match_rate = self._catalog_match_rate(fast_dets)

        metadata["attempts"].append({
            "engine": "local-fast-screen-speculative",
            "detections": local_items,
            "confidence": round(raw_conf, 4),
            "calibrated_confidence": round(calibrated_conf, 4),
            "catalog_match_rate": round(catalog_match_rate, 4),
            "screen_ms": screen_ms,
        })

        effective_threshold = LOCAL_CONFIDENCE_SKIP_THRESHOLD
        if not is_structured:
            effective_threshold = max(effective_threshold, 0.88)

        skip_azure = (
            calibrated_conf >= effective_threshold
            and local_items >= LOCAL_MIN_DETECTIONS_SKIP
            and catalog_match_rate >= LOCAL_CATALOG_MATCH_SKIP_THRESHOLD
        )

        def _build_azure_result(spec):
            """Build a unified result dict from speculative Azure output."""
            azure_res = spec["result"]
            azure_items = azure_res.get("items", [])
            azure_dets = azure_res.get("ocr_detections", [])
            model = spec["model"]

            avg_conf = (
                sum(i.get("confidence", 0) for i in azure_items) / len(azure_items)
                if azure_items
                else self._avg_confidence(azure_dets)
            )
            total_ms = int((time.time() - total_start) * 1000)

            _has_meta = bool(
                azure_items or azure_res.get("total")
                or azure_res.get("subtotal") or azure_res.get("merchant")
            )
            r = {
                "engine_used": model,
                "ocr_detections": azure_dets,
                "azure_structured": azure_res if _has_meta else None,
                "ocr_time_ms": total_ms,
                "ocr_passes": 1,
                "confidence_avg": round(avg_conf, 4),
                "metadata": {**metadata, "azure_pages_used": spec["pages"]},
            }

            # Cache result (only if quality is high enough)
            _cache_worthy = len(azure_dets) >= 2 or (azure_items and avg_conf > 0.5)
            if cache_key and _cache_worthy:
                with contextlib.suppress(Exception):
                    self.image_cache.put(cache_key, r)

            return r

        if skip_azure:
            # Screen says local is good enough
            # Check if Azure already finished (use it — already paid for!)
            if azure_future.done():
                try:
                    spec = azure_future.result(timeout=0)
                    if spec["status"] == "success":
                        result = _build_azure_result(spec)
                        result["metadata"]["strategy"] = "speculative-azure-bonus"
                        logger.info(
                            f"[Hybrid] ✅ Speculative Azure DONE in {screen_ms}ms — "
                            f"using Azure result (already paid): "
                            f"{len(spec['result'].get('ocr_detections', []))} dets"
                        )
                        pool.shutdown(wait=False)
                        return result
                except Exception:
                    pass

            # Azure not done or failed — use full local pipeline
            pool.shutdown(wait=False)  # Let Azure finish in background (page consumed)
            logger.info(
                f"[Hybrid] ✅ Fast screen GOOD in {screen_ms}ms "
                f"(cal_conf={calibrated_conf:.3f}, items={local_items}). "
                f"Speculative Azure still running — using local pipeline"
            )
            local_result = self._run_local_pipeline(
                processed_image, image_path, is_structured,
                original_color=original_color, quality_info=quality_info,
            )
            total_ms = int((time.time() - total_start) * 1000)
            local_result["ocr_time_ms"] = total_ms
            local_result["metadata"] = {
                **metadata,
                "strategy": "speculative-local-skip",
                "azure_pages_used": 1,  # Page consumed by speculative call
            }
            return local_result

        else:
            # Screen says insufficient — wait for Azure result (already in-flight!)
            logger.info(
                f"[Hybrid] Fast screen INSUFFICIENT in {screen_ms}ms "
                f"(cal_conf={calibrated_conf:.3f}, items={local_items}) — "
                f"waiting for speculative Azure..."
            )
            try:
                spec = azure_future.result(timeout=AZURE_API_TIMEOUT)
                pool.shutdown(wait=False)

                if spec["status"] == "success":
                    result = _build_azure_result(spec)
                    result["metadata"]["strategy"] = "speculative-azure-direct"
                    total_ms = int((time.time() - total_start) * 1000)
                    result["ocr_time_ms"] = total_ms
                    logger.info(
                        f"[Hybrid] ✅ Speculative Azure SUCCESS: {spec['model']}, "
                        f"{len(spec['result'].get('ocr_detections', []))} dets, "
                        f"{total_ms}ms total"
                    )
                    return result

                elif spec["status"] == "blocked":
                    logger.warning(f"[Hybrid] Azure BLOCKED in speculative: {spec.get('reason')}")
                else:
                    logger.warning(f"[Hybrid] Speculative Azure error: {spec.get('error')}")

            except Exception as e:
                pool.shutdown(wait=False)
                logger.warning(f"[Hybrid] Speculative Azure timeout/error: {e}")

            # Azure failed/blocked — run full local pipeline
            local_result = self._run_local_pipeline(
                processed_image, image_path, is_structured,
                original_color=original_color, quality_info=quality_info,
            )
            total_ms = int((time.time() - total_start) * 1000)
            local_result["ocr_time_ms"] = total_ms
            local_result["metadata"] = {
                **metadata,
                "strategy": "speculative-fallback-local",
                "azure_pages_used": 0,
            }
            return local_result

    def _run_azure_pipeline(
        self,
        image_path: str,
        processed_image,
        is_structured: bool,
    ) -> dict:
        """
        AZURE-ONLY mode: Use Azure, fail if unavailable.

        Now respects AZURE_MODEL_STRATEGY, checks usage limits,
        and uses image cache — same cost controls as auto mode.
        """
        from app.config import AZURE_MODEL_STRATEGY, AZURE_RECEIPT_MIN_ITEMS

        if self.azure_engine is None:
            raise RuntimeError(
                "OCR_ENGINE_MODE is 'azure' but Azure Document Intelligence "
                "is not configured. Set AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT "
                "and AZURE_DOCUMENT_INTELLIGENCE_KEY environment variables."
            )

        total_start = time.time()
        metadata = {"strategy": "azure-only", "azure_pages_used": 0}

        # ── Cache check (prevents duplicate charges) ──
        cache_key = None
        try:
            cache_key = self.image_cache.compute_hash(image_path)
            cached_result = self.image_cache.get(cache_key)
            if cached_result is not None:
                import copy
                result_copy = copy.deepcopy(cached_result)
                total_ms = int((time.time() - total_start) * 1000)
                result_copy["ocr_time_ms"] = total_ms
                result_copy["metadata"] = {"strategy": "azure-only-cached", "cache": "hit", "azure_pages_used": 0}
                logger.info(f"[Azure-Only] ✅ Cache HIT — saved Azure page ({total_ms}ms)")
                return result_copy
        except Exception as e:
            logger.debug(f"[Azure-Only] Cache check failed: {e}")

        # ── Usage limit check ──
        try:
            usage_check = self.usage_tracker.can_call_azure()
            if isinstance(usage_check, dict) and not usage_check.get("allowed", False):
                raise RuntimeError(
                    f"Azure usage limit reached: {usage_check.get('reason', 'limit exceeded')}. "
                    f"Switch to OCR_ENGINE_MODE=auto for local fallback."
                )
        except RuntimeError:
            raise
        except Exception as e:
            logger.debug(f"[Azure-Only] Usage check failed: {e}")

        # ── Call Azure respecting model strategy ──
        if AZURE_MODEL_STRATEGY == "receipt-only":
            azure_result = self.azure_engine.extract_receipt_structured(image_path)
            azure_items = azure_result.get("items", [])
            azure_detections = azure_result.get("ocr_detections", [])
            model_used = "azure-receipt"
            metadata["azure_pages_used"] = 1

        elif AZURE_MODEL_STRATEGY == "receipt-then-read":
            azure_result = self.azure_engine.extract_receipt_structured(image_path)
            azure_items = azure_result.get("items", [])
            azure_detections = azure_result.get("ocr_detections", [])
            model_used = "azure-receipt"
            metadata["azure_pages_used"] = 1

            if len(azure_items) < AZURE_RECEIPT_MIN_ITEMS:
                # Check usage limit before burning a second Azure page
                can_call_read = True
                try:
                    if hasattr(self, 'usage_tracker') and self.usage_tracker:
                        usage_check = self.usage_tracker.can_call_azure()
                        can_call_read = usage_check.get("allowed", True) if isinstance(usage_check, dict) else bool(usage_check)
                except Exception:
                    pass
                if can_call_read:
                    try:
                        read_detections = self.azure_engine.extract_text_read(image_path)
                        if read_detections:
                            azure_detections = read_detections
                            model_used = "azure-read"
                            metadata["azure_pages_used"] = 2
                    except Exception as e:
                        logger.warning("[Azure-Only] Read fallback failed: %s, using receipt model results", e)
                else:
                    logger.info("[Azure-Only] Skipping read fallback: Azure usage limit reached")
        else:
            # Default: read-only (cheapest)
            read_detections = self.azure_engine.extract_text_read(image_path)
            azure_result = {"items": [], "ocr_detections": read_detections}
            azure_items = []
            azure_detections = read_detections
            model_used = "azure-read"
            metadata["azure_pages_used"] = 1

        avg_conf = (
            sum(i.get("confidence", 0) for i in azure_items) / len(azure_items)
            if azure_items
            else self._avg_confidence(azure_detections)
        )
        total_ms = int((time.time() - total_start) * 1000)

        # Preserve azure_structured even without items if it has useful metadata
        _has_useful_meta = bool(
            azure_items
            or azure_result.get("total")
            or azure_result.get("subtotal")
            or azure_result.get("merchant")
            or azure_result.get("transaction_date")
        )
        result = {
            "engine_used": model_used,
            "ocr_detections": azure_detections,
            "azure_structured": azure_result if _has_useful_meta else None,
            "ocr_time_ms": total_ms,
            "ocr_passes": 1,
            "confidence_avg": round(avg_conf, 4),
            "metadata": metadata,
        }

        # ── Cache result (only if quality is high enough — no bad-result lock-in) ──
        _cache_worthy = (
            len(azure_detections) >= 2
            or (azure_items and avg_conf > 0.5)
        )
        if cache_key and _cache_worthy:
            with contextlib.suppress(Exception):
                self.image_cache.put(cache_key, result)
        elif cache_key:
            logger.debug(
                "[Azure-Only] Skipping cache write — result too sparse "
                f"(detections={len(azure_detections)}, conf={avg_conf:.2f})"
            )

        logger.info(
            f"[Azure-Only] ✅ {model_used}, {len(azure_detections)} detections, "
            f"{metadata['azure_pages_used']} page(s), {total_ms}ms"
        )
        return result

    def _run_local_pipeline(
        self,
        processed_image,
        image_path: str,
        is_structured: bool,
        original_color=None,
        quality_info: dict = None,
    ) -> dict:
        """
        LOCAL mode: EasyOCR multi-pass strategy with parallel dual-pass.

        Phase 1 (gray fast) and Phase 2 (color full) run concurrently via
        ThreadPoolExecutor when OCR_PARALLEL_DUAL_PASS=True, cutting local
        scan time by ~40% for the common case where both passes are needed.

        Args:
            original_color: Pre-loaded color image (BGR).  When supplied the
                            expensive disk-read + EXIF correction is skipped.
        """
        from concurrent.futures import ThreadPoolExecutor

        import cv2

        from app.config import (
            IMAGE_MAX_DIMENSION,
            OCR_PARALLEL_DUAL_PASS,
            OCR_SMART_PASS_THRESHOLD,
        )

        total_start = time.time()
        metadata = {"strategy": "local"}
        ocr_engine = self.local_engine

        # If no preprocessed image provided, do basic preprocessing
        if processed_image is None:
            from app.ocr.preprocessor import ImagePreprocessor
            preprocessor = ImagePreprocessor()
            processed_image, _ = preprocessor.preprocess(image_path)

        # Crop to content region (skip if document scan already cropped)
        from app.ocr.preprocessor import ImagePreprocessor
        cropped_gray = ImagePreprocessor.crop_to_content_static(processed_image)

        # ── Structured receipts: single turbo pass, no color needed ──
        if is_structured:
            logger.debug("[Local] ⚡ TURBO mode for structured receipt")
            phase1_start = time.time()
            ocr_results = ocr_engine.extract_text_turbo(cropped_gray)
            phase1_ms = int((time.time() - phase1_start) * 1000)
            logger.info(f"[Local] Turbo pass: {len(ocr_results)} detections in {phase1_ms}ms")
            total_ms = int((time.time() - total_start) * 1000)
            return {
                "engine_used": "local",
                "ocr_detections": ocr_results,
                "azure_structured": None,
                "ocr_time_ms": total_ms,
                "ocr_passes": 1,
                "confidence_avg": round(self._avg_confidence(ocr_results), 4),
                "metadata": metadata,
            }

        # ── Load color image for Phase 2 (reuse if already provided) ──
        # The caller (receipt_service) passes the preprocessor's _color_image
        # which is already EXIF-corrected and resized.  Only fall back to
        # disk re-read if the caller didn't provide one (shouldn't happen
        # in normal flow — this saves 200-500ms on 5MB phone photos).
        if original_color is None:
            logger.debug("[Local] ⚠ Color image not provided — reading from disk (slow path)")
            try:
                original_color = cv2.imread(image_path)
            except Exception:
                original_color = None
            if original_color is not None:
                h, w = original_color.shape[:2]
                if max(h, w) > IMAGE_MAX_DIMENSION:
                    scale = IMAGE_MAX_DIMENSION / max(h, w)
                    original_color = cv2.resize(original_color, None, fx=scale, fy=scale,
                                                interpolation=cv2.INTER_AREA)

        # ── TRUE PARALLEL dual-pass: both passes run simultaneously ──
        # When OCR_PARALLEL_DUAL_PASS=True, launch gray and color passes
        # concurrently via ThreadPoolExecutor. PyTorch releases the GIL
        # during neural-net forward passes, so true parallelism is achieved.
        # This cuts local OCR time by ~40% (max(gray,color) instead of sum).
        if OCR_PARALLEL_DUAL_PASS and original_color is not None:
            ocr_engine_2 = self.local_engine_2
            parallel_start = time.time()

            with ThreadPoolExecutor(max_workers=2) as executor:
                future_gray = executor.submit(ocr_engine.extract_text_fast, cropped_gray)
                future_color = executor.submit(ocr_engine_2.extract_text, original_color, quality_info)

                gray_results = future_gray.result()
                color_results = future_color.result()

            parallel_ms = int((time.time() - parallel_start) * 1000)
            logger.info(
                f"[Local] ⚡ Parallel dual-pass done in {parallel_ms}ms "
                f"(gray={len(gray_results)}, color={len(color_results)} detections)"
            )

            # Pick the better pass; merge in the other as supplementary
            gray_conf = self._avg_confidence(gray_results)
            color_conf = self._avg_confidence(color_results)
            if len(color_results) > len(gray_results) or (
                len(color_results) == len(gray_results) and color_conf > gray_conf
            ):
                primary, secondary = color_results, gray_results
            else:
                primary, secondary = gray_results, color_results

            ocr_results = self._merge_local_passes(primary, secondary)
            total_ms = int((time.time() - total_start) * 1000)
            return {
                "engine_used": "local",
                "ocr_detections": ocr_results,
                "azure_structured": None,
                "ocr_time_ms": total_ms,
                "ocr_passes": 2,
                "confidence_avg": round(self._avg_confidence(ocr_results), 4),
                "metadata": {**metadata, "dual_pass": "true-parallel"},
            }

        # ── SERIAL fallback (OCR_PARALLEL_DUAL_PASS=False or no color image) ──
        logger.debug("[Local] Serial single/dual-pass")
        phase1_start = time.time()
        gray_results = ocr_engine.extract_text_fast(cropped_gray)
        phase1_ms = int((time.time() - phase1_start) * 1000)
        logger.info(f"[Local] Phase 1: {len(gray_results)} detections in {phase1_ms}ms")

        quick_items = self._quick_item_count_local(gray_results)
        gray_conf = self._avg_confidence(gray_results)

        # Smart-pass decision: skip 2nd pass if first pass is strong enough.
        # For same-type receipts, the first pass almost always captures everything.
        # Two conditions to skip: enough catalog items found AND decent confidence.
        skip_second = (
            quick_items >= OCR_SMART_PASS_THRESHOLD
            and gray_conf >= 0.55
        )
        if skip_second:
            logger.info(
                f"[Local] ⚡ Smart skip: {quick_items} items found, "
                f"conf={gray_conf:.3f} → single-pass sufficient"
            )

        alt_results = []
        if not skip_second and original_color is not None:
            logger.info(f"[Local] Phase 2: only {quick_items} items (conf={gray_conf:.3f}), running color pass...")
            phase2_start = time.time()
            color_results = ocr_engine.extract_text(original_color, quality_info=quality_info)
            phase2_ms = int((time.time() - phase2_start) * 1000)
            logger.info(f"[Local] Phase 2: {len(color_results)} detections in {phase2_ms}ms")

            gray_conf  = self._avg_confidence(gray_results)
            color_conf = self._avg_confidence(color_results)
            if len(color_results) > len(gray_results) or (
                len(color_results) == len(gray_results) and color_conf > gray_conf
            ):
                alt_results = gray_results
                ocr_results = color_results
            else:
                alt_results = color_results
                ocr_results = gray_results
        else:
            ocr_results = gray_results

        if alt_results:
            ocr_results = self._merge_local_passes(ocr_results, alt_results)

        total_ms = int((time.time() - total_start) * 1000)
        return {
            "engine_used": "local",
            "ocr_detections": ocr_results,
            "azure_structured": None,
            "ocr_time_ms": total_ms,
            "ocr_passes": 2 if alt_results else 1,
            "confidence_avg": round(self._avg_confidence(ocr_results), 4),
            "metadata": {**metadata, "dual_pass": "serial"},
        }

    # ─── Helper Methods ──────────────────────────────────────────────────

    def _check_image_quality(self, processed_image) -> dict:
        """
        Quick image quality assessment to prevent wasting Azure pages on bad images.

        Checks:
            - Sharpness (Laplacian variance): detects blur
            - Brightness (mean pixel value): detects too dark / too bright

        Returns:
            {"acceptable": bool, "sharpness": float, "brightness": float, "reason": str}
        """
        import cv2
        import numpy as np

        from app.config import IMAGE_QUALITY_MIN_BRIGHTNESS, IMAGE_QUALITY_MIN_SHARPNESS

        result = {"acceptable": True, "sharpness": 0.0, "brightness": 0.0, "reason": ""}

        try:
            img = processed_image
            if img is None or img.size == 0:
                result["acceptable"] = False
                result["reason"] = "Empty image"
                return result

            # Ensure grayscale
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img

            # Downscale for faster Laplacian (~4× speedup on 1600px images)
            h, w = gray.shape[:2]
            _qg_target = 500
            if max(h, w) > _qg_target:
                _qg_scale = _qg_target / max(h, w)
                gray_small = cv2.resize(gray, None, fx=_qg_scale, fy=_qg_scale,
                                        interpolation=cv2.INTER_AREA)
            else:
                gray_small = gray

            # Sharpness: Laplacian variance (higher = sharper)
            laplacian = cv2.Laplacian(gray_small, cv2.CV_64F)
            result["sharpness"] = float(laplacian.var())

            # Brightness: mean pixel value (on downscaled — scale-invariant)
            result["brightness"] = float(np.mean(gray_small))

            # Check thresholds
            reasons = []
            if result["sharpness"] < IMAGE_QUALITY_MIN_SHARPNESS:
                reasons.append(f"Too blurry (sharpness={result['sharpness']:.1f}, need >={IMAGE_QUALITY_MIN_SHARPNESS})")

            if result["brightness"] < IMAGE_QUALITY_MIN_BRIGHTNESS:
                reasons.append(f"Too dark (brightness={result['brightness']:.0f}, need >={IMAGE_QUALITY_MIN_BRIGHTNESS})")
            elif result["brightness"] > 252:
                reasons.append(f"Overexposed (brightness={result['brightness']:.0f})")

            if reasons:
                result["acceptable"] = False
                result["reason"] = "; ".join(reasons)
                logger.info(f"[QualityGate] Image REJECTED: {result['reason']}")
            else:
                logger.debug(
                    f"[QualityGate] Image OK: sharpness={result['sharpness']:.1f}, "
                    f"brightness={result['brightness']:.0f}"
                )

        except Exception as e:
            logger.debug(f"[QualityGate] Check failed: {e}")
            result["acceptable"] = True  # Don't block on check failure

        return result

    def _merge_local_passes(
        self, primary: list[dict], secondary: list[dict]
    ) -> list[dict]:
        """Merge primary and secondary local OCR passes with multi-pass voting.

        Voting logic:
        1. Both passes detect the same text at the same Y-position →
           AGREE: boost confidence by 15%, mark as verified.
        2. Only one pass detects a text → SOLE: keep at original confidence,
           flag for review if confidence is marginal.
        3. Both detect text at the same Y-position but DIFFERENT text →
           CONFLICT: pick the higher-confidence one, flag for review.

        Dedup key is (text_upper, y_bucket) — NOT just text — so the same
        product code that legitimately appears on two different rows is
        preserved as two distinct detections.
        y_bucket = round(y_center / 60) gives 60-pixel row bands.
        """
        # Fast path: if secondary is empty, return primary as-is
        if not secondary:
            return primary
        if not primary:
            return secondary
        def _det_key(det: dict):
            text_upper = det["text"].upper().strip()
            bbox = det.get("bbox", [])
            try:
                y_mid = (float(bbox[0][1]) + float(bbox[2][1])) / 2
            except (IndexError, TypeError, ValueError):
                y_mid = 0.0
            y_bucket = round(y_mid / 60)
            return (text_upper, y_bucket)

        def _y_bucket(det: dict):
            bbox = det.get("bbox", [])
            try:
                y_mid = (float(bbox[0][1]) + float(bbox[2][1])) / 2
            except (IndexError, TypeError, ValueError):
                y_mid = 0.0
            return round(y_mid / 60)

        # Build maps: y_bucket → list of detections for conflict detection
        primary_by_y = {}
        primary_keys = set()
        for det in primary:
            key = _det_key(det)
            yb = key[1]
            primary_keys.add(key)
            primary_by_y.setdefault(yb, []).append(det)

        secondary_by_y = {}
        secondary_keys = set()
        for det in secondary:
            key = _det_key(det)
            yb = key[1]
            secondary_keys.add(key)
            secondary_by_y.setdefault(yb, []).append(det)

        # Build best detection per key (text+y_bucket)
        best_detections = {}
        for det in primary:
            key = _det_key(det)
            if key not in best_detections or det["confidence"] > best_detections[key]["confidence"]:
                best_detections[key] = det

        for det in secondary:
            key = _det_key(det)
            if key not in best_detections or det["confidence"] > best_detections[key]["confidence"]:
                best_detections[key] = det

        # ── Voting: agreement vs sole vs conflict ──
        agreed_keys = primary_keys & secondary_keys
        sole_primary = primary_keys - secondary_keys
        sole_secondary = secondary_keys - primary_keys

        # Agreement: both passes saw the same text at the same Y → strong signal
        for key in agreed_keys:
            det = best_detections[key]
            det["confidence"] = round(min(1.0, det["confidence"] * 1.15), 4)
            det["needs_review"] = False
            det["vote_status"] = "agreed"

        # Sole detections: only one pass saw this — check for conflicts
        all_sole = sole_primary | sole_secondary
        for key in all_sole:
            det = best_detections[key]
            yb = key[1]
            text_upper = key[0]

            # Check if the OTHER pass has a DIFFERENT text at the same y_bucket
            # (conflict: both saw something in the same row but read different text)
            other_y_texts = set()
            if key in sole_primary:
                for odet in secondary_by_y.get(yb, []):
                    other_y_texts.add(odet["text"].upper().strip())
            else:
                for odet in primary_by_y.get(yb, []):
                    other_y_texts.add(odet["text"].upper().strip())

            if other_y_texts and text_upper not in other_y_texts:
                # CONFLICT: same row, different text → flag for review
                det["needs_review"] = True
                det["vote_status"] = "conflict"
                # Slight confidence penalty for conflicts
                det["confidence"] = round(det["confidence"] * 0.95, 4)
            else:
                # Sole detection, no conflict — keep at original confidence
                det["vote_status"] = "sole"
                # Only flag low-confidence sole detections for review
                if det["confidence"] < 0.75:
                    det["needs_review"] = True

        merged = list(best_detections.values())

        # Sort by Y-position to maintain reading order
        def y_sort_key(d):
            bbox = d.get("bbox", [])
            if bbox and len(bbox) >= 3:
                try:
                    return (float(bbox[0][1]) + float(bbox[2][1])) / 2
                except (IndexError, TypeError, ValueError):
                    pass
            return 0
        merged.sort(key=y_sort_key)

        # ── Post-merge dedup: collapse same-text detections at adjacent y-buckets ──
        # Multi-pass can produce two detections of the same text at slightly
        # different Y positions (different buckets). Keep the higher-confidence one.
        text_groups: dict = {}
        for det in merged:
            text_upper = det["text"].upper().strip()
            text_groups.setdefault(text_upper, []).append(det)

        deduped = []
        for text, dets in text_groups.items():
            if len(dets) <= 1:
                deduped.extend(dets)
                continue
            # Sort by Y position
            dets.sort(key=y_sort_key)
            is_short_digit = bool(re.match(r'^\d{1,2}$', text.strip()))
            keep = [dets[0]]
            for d in dets[1:]:
                cur_y = y_sort_key(d)
                prev_y = y_sort_key(keep[-1])
                y_dist = abs(cur_y - prev_y)
                if is_short_digit:
                    # ── Short digits (quantities): use raw Y-distance ──
                    # Bucket-based adjacency causes cascading collapse when
                    # the same digit appears on many consecutive rows (e.g.,
                    # qty "1" on 4 rows at buckets 4,5,6,7 all merge into one).
                    # Instead, only collapse if within 35px (same physical text
                    # read at slightly different Y by two passes).
                    if y_dist < 35:
                        if d["confidence"] > keep[-1]["confidence"]:
                            keep[-1] = d
                    else:
                        keep.append(d)
                else:
                    prev_yb = _y_bucket(keep[-1])
                    cur_yb = _y_bucket(d)
                    if abs(cur_yb - prev_yb) <= 1:
                        # Adjacent y-buckets, same text → multi-pass duplicate
                        if d["confidence"] > keep[-1]["confidence"]:
                            keep[-1] = d
                    else:
                        # Genuinely different line → keep both
                        keep.append(d)
            deduped.extend(keep)
        merged = sorted(deduped, key=y_sort_key)

        # ── Position-based dedup: collapse echo detections at the same (x, y) ──
        # When two passes read the SAME physical text differently (e.g.,
        # "TEWA" and "TEW4"), they appear at nearly identical (x, y) positions
        # but with different text and y-buckets.  Without this step they'd
        # both survive the text-based dedup and produce a bogus merged line
        # like "TEW4 TEWA" that confuses the parser.
        def _det_xy(det):
            bbox = det.get("bbox", [])
            try:
                x = (float(bbox[0][0]) + float(bbox[2][0])) / 2
                y = (float(bbox[0][1]) + float(bbox[2][1])) / 2
                return x, y
            except (IndexError, TypeError, ValueError):
                return 0.0, 0.0

        pos_deduped = []
        consumed_pos = set()
        for i, det in enumerate(merged):
            if i in consumed_pos:
                continue
            xi, yi = _det_xy(det)
            best = det
            for j in range(i + 1, len(merged)):
                if j in consumed_pos:
                    continue
                xj, yj = _det_xy(merged[j])
                # Same physical position: within 30px X and 15px Y
                if abs(xi - xj) < 30 and abs(yi - yj) < 15:
                    consumed_pos.add(j)
                    if merged[j]["confidence"] > best["confidence"]:
                        best = merged[j]
                    logger.debug(
                        f"[Merge] Position dedup: '{det['text']}' vs '{merged[j]['text']}' "
                        f"at ({xi:.0f},{yi:.0f}) — keeping '{best['text']}'"
                    )
            pos_deduped.append(best)
        if len(pos_deduped) < len(merged):
            logger.info(
                f"[Merge] Position dedup removed {len(merged) - len(pos_deduped)} "
                f"echo detections"
            )
        merged = pos_deduped

        # Log voting summary
        n_agreed = sum(1 for d in merged if d.get("vote_status") == "agreed")
        n_sole = sum(1 for d in merged if d.get("vote_status") == "sole")
        n_conflict = sum(1 for d in merged if d.get("vote_status") == "conflict")
        logger.info(
            f"[Merge] Multi-pass vote: {n_agreed} agreed, "
            f"{n_sole} sole, {n_conflict} conflicts, "
            f"{len(merged)} total detections"
        )

        return merged

    def _cross_verify_results(
        self, local_result: dict, azure_detections: list[dict]
    ) -> dict:
        """
        Cross-verify local OCR results with Azure Read detections.
        Boosts confidence of items found by both engines.
        Flags items found by only one engine for review.
        """
        local_dets = local_result.get("ocr_detections", [])
        azure_texts = {d["text"].upper().strip() for d in azure_detections}
        # Also build a set of individual tokens from Azure (word-level) for
        # token-level matching when Azure returns word-level detections
        # but EasyOCR returns full-line detections.
        azure_tokens = set()
        for t in azure_texts:
            for tok in t.split():
                tok = tok.strip()
                if len(tok) >= 3:  # Skip very short tokens (noise)
                    azure_tokens.add(tok)

        for det in local_dets:
            local_text = det["text"].upper().strip()
            # 1. Exact match (same granularity)
            if local_text in azure_texts:
                det["confidence"] = min(1.0, det["confidence"] * 1.15)
                det["needs_review"] = False
                det["cross_verified"] = True
                continue
            # 2. Token-level match: check if most local tokens appear in Azure
            local_tokens = [tok.strip() for tok in local_text.split() if len(tok.strip()) >= 3]
            if local_tokens:
                matched = sum(1 for tok in local_tokens if tok in azure_tokens)
                match_ratio = matched / len(local_tokens)
                if match_ratio >= 0.5:  # At least half of tokens match
                    boost = 1.0 + (0.15 * match_ratio)  # Proportional boost
                    det["confidence"] = min(1.0, det["confidence"] * boost)
                    det["needs_review"] = False
                    det["cross_verified"] = True
                    continue
            # 3. Substring match: Azure word contained in local line
            if any(az_text in local_text for az_text in azure_texts if len(az_text) >= 4):
                det["confidence"] = min(1.0, det["confidence"] * 1.08)
                det["cross_verified"] = True
                continue
            # No match found
            det["cross_verified"] = False

        local_result["engine_used"] = "hybrid-cross-verified"
        local_result["metadata"]["cross_verified"] = True
        return local_result

    def _quick_item_count_local(self, ocr_results: list[dict]) -> int:
        """Quick-count catalog items from OCR results (used for pass decisions)."""
        try:
            # Use the parser's in-memory catalog instead of DB query for speed
            from app.services.receipt_service import receipt_service
            catalog = receipt_service.parser.product_catalog
        except Exception:
            try:
                from app.services.product_service import product_service
                catalog = product_service.get_product_code_map()
                # Uppercase keys for matching
                catalog = {k.upper(): v for k, v in catalog.items()}
            except Exception:
                return 0

        found = set()
        for r in ocr_results:
            text = r.get("text", "").upper().strip()
            for token in text.split():
                # Keep alphanumeric chars (not just alpha) so TEW1, PEPW10 match
                clean = ''.join(c for c in token if c.isalnum()).upper()
                if 2 <= len(clean) <= 7 and clean in catalog:
                    found.add(clean)
        return len(found)

    def _avg_confidence(self, detections: list[dict]) -> float:
        """Calculate average confidence across detections.
        Returns 0.0 for empty lists — callers should check len(detections)
        separately to distinguish 'no data' from 'low confidence'."""
        if not detections:
            return 0.0
        confs = [d.get("confidence", 0) for d in detections]
        return sum(confs) / len(confs)

    def _calibrated_avg_confidence(self, detections: list[dict]) -> float:
        """Calculate average CALIBRATED confidence across detections.

        Uses OCREngine.calibrate_confidence() to adjust each detection's
        raw EasyOCR score for text quality indicators (length, noise chars,
        repetition). This produces a more realistic confidence that the
        routing logic can trust for skip/Azure decisions.
        """
        if not detections:
            return 0.0
        from app.ocr.engine import OCREngine
        cal_confs = [
            OCREngine.calibrate_confidence(d.get("text", ""), d.get("confidence", 0))
            for d in detections
        ]
        return sum(cal_confs) / len(cal_confs)

    def _catalog_match_rate(self, detections: list[dict]) -> float:
        """Calculate what fraction of OCR detections match known product codes.

        A high raw confidence is meaningless if the detected text doesn't
        match any products in the catalog. This ratio helps the routing
        logic decide whether to trust local OCR or escalate to Azure.

        Returns:
            Float 0.0-1.0 representing the fraction of detections that
            contain at least one catalog product code match.
        """
        if not detections:
            return 0.0

        try:
            from app.services.receipt_service import receipt_service
            catalog = receipt_service.parser.product_catalog
        except Exception:
            try:
                from app.services.product_service import product_service
                catalog = product_service.get_product_code_map()
                catalog = {k.upper(): v for k, v in catalog.items()}
            except Exception:
                return 0.0  # Can't check catalog — pessimistic → route to Azure

        if not catalog:
            # Empty catalog — can't verify, but DON'T penalize local OCR.
            # Return 1.0 so new installations don't burn Azure budget on every
            # scan just because the product catalog hasn't been populated yet.
            return 1.0

        matched = 0
        # Only consider detections that are likely product lines (3+ chars, has alpha)
        candidate_dets = [
            d for d in detections
            if len(d.get("text", "").strip()) >= 3
            and any(c.isalpha() for c in d.get("text", ""))
        ]

        if not candidate_dets:
            return 0.0

        for det in candidate_dets:
            text = det.get("text", "").upper().strip()
            for token in text.split():
                clean = ''.join(c for c in token if c.isalnum()).upper()
                if 2 <= len(clean) <= 7 and clean in catalog:
                    matched += 1
                    break

        return matched / len(candidate_dets)

    def get_engine_status(self) -> dict:
        """
        Get current engine status for dashboard/debugging.

        Returns info about which engines are available, configured,
        plus usage tracking and cache performance.
        """
        from app.config import AZURE_DOC_INTEL_AVAILABLE

        status = {
            "mode": self.mode,
            "azure_configured": AZURE_DOC_INTEL_AVAILABLE,
            "azure_connected": self._azure_ok,
            "local_loaded": self._local_engine is not None,
            "recommended_mode": "auto" if AZURE_DOC_INTEL_AVAILABLE else "local",
        }

        if AZURE_DOC_INTEL_AVAILABLE and not self._azure_checked:
            status["azure_status"] = "not_tested_yet"
        elif self._azure_ok:
            status["azure_status"] = "connected"
        else:
            status["azure_status"] = "unavailable"

        # Add usage info
        try:
            status["usage"] = self.usage_tracker.get_usage_summary()
        except Exception:
            status["usage"] = None

        # Add cache info
        try:
            status["cache"] = self.image_cache.get_stats()
        except Exception:
            status["cache"] = None

        return status


# ─── Singleton ───────────────────────────────────────────────────────────────

_hybrid_engine: HybridOCREngine | None = None
_hybrid_engine_lock = threading.Lock()


def get_hybrid_engine() -> HybridOCREngine:
    """Get or create the hybrid OCR engine singleton. Thread-safe."""
    global _hybrid_engine
    if _hybrid_engine is None:
        with _hybrid_engine_lock:
            if _hybrid_engine is None:
                _hybrid_engine = HybridOCREngine()
    return _hybrid_engine
