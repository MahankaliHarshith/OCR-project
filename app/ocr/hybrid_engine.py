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

import logging
import re
import time
from typing import Dict, List, Optional, Tuple
from pathlib import Path

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
        if self._local_engine_2 is None:
            from app.ocr.engine import OCREngine
            from app.config import OCR_LANGUAGE, OCR_USE_GPU
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
    ) -> Dict:
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
        total_start = time.time()

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
    ) -> Dict:
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
            AZURE_RECEIPT_MIN_ITEMS,
            AZURE_RECEIPT_CONFIDENCE_THRESHOLD,
            AZURE_MODEL_STRATEGY,
            HYBRID_CROSS_VERIFY,
            LOCAL_CONFIDENCE_SKIP_THRESHOLD,
            LOCAL_MIN_DETECTIONS_SKIP,
            LOCAL_CATALOG_MATCH_SKIP_THRESHOLD,
            IMAGE_QUALITY_GATE_ENABLED,
            IMAGE_QUALITY_MIN_SHARPNESS,
            IMAGE_QUALITY_MIN_BRIGHTNESS,
        )

        total_start = time.time()
        metadata = {"strategy": "auto", "attempts": [], "azure_pages_used": 0}

        # ── Step 0: Check image cache (FREE — prevents duplicate Azure bills) ──
        cache_key = None
        try:
            cache_key = self.image_cache.compute_hash(image_path)
            cached_result = self.image_cache.get(cache_key)
            if cached_result is not None:
                total_ms = int((time.time() - total_start) * 1000)
                cached_result["ocr_time_ms"] = total_ms
                cached_result["metadata"] = {"strategy": "auto-cached", "cache": "hit", "azure_pages_used": 0}
                logger.info(f"[Hybrid] ✅ Cache HIT — saved 1 Azure page ({total_ms}ms)")
                return cached_result
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

        # ── Step 2: Run local OCR FIRST (always free, fast) ─────────────
        local_result = None
        if processed_image is not None:
            try:
                local_result = self._run_local_pipeline(processed_image, image_path, is_structured, original_color=original_color, quality_info=quality_info)
                local_conf = local_result.get("confidence_avg", 0)
                local_items = len(local_result.get("ocr_detections", []))

                # ── Calibrated confidence: adjusts for EasyOCR's inflated scores ──
                # EasyOCR often reports 0.70-0.90 on garbled handwritten text.
                # Calibration penalizes short/noisy/repetitive detections.
                from app.ocr.engine import OCREngine
                local_dets = local_result.get("ocr_detections", [])
                calibrated_conf = self._calibrated_avg_confidence(local_dets)

                # ── Catalog match rate: what % of detections match known products ──
                # High raw confidence on garbage text means nothing if the words
                # don't match any product codes in the catalog.
                catalog_match_rate = self._catalog_match_rate(local_dets)

                metadata["attempts"].append({
                    "engine": "local-first",
                    "detections": local_items,
                    "confidence": round(local_conf, 4),
                    "calibrated_confidence": round(calibrated_conf, 4),
                    "catalog_match_rate": round(catalog_match_rate, 4),
                })

                # Smart skip: THREE conditions must ALL be met to skip Azure:
                #   1. Calibrated confidence >= threshold (genuinely well-read text)
                #   2. Enough detections found (it actually found content)
                #   3. Catalog match rate >= threshold (detected text matches known products)
                # For handwritten receipts, also require higher confidence since
                # EasyOCR is weaker on handwriting than on printed text.
                effective_threshold = LOCAL_CONFIDENCE_SKIP_THRESHOLD
                if not is_structured:
                    # Handwritten: require even higher confidence to skip Azure
                    effective_threshold = max(effective_threshold, 0.88)

                skip_azure = (
                    calibrated_conf >= effective_threshold
                    and local_items >= LOCAL_MIN_DETECTIONS_SKIP
                    and catalog_match_rate >= LOCAL_CATALOG_MATCH_SKIP_THRESHOLD
                )

                if skip_azure:
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
                        f"(cal_conf={calibrated_conf:.3f}, raw_conf={local_conf:.3f}, "
                        f"items={local_items}, catalog={catalog_match_rate:.1%}), "
                        f"Azure page SAVED ({total_ms}ms)"
                    )
                    return local_result
                else:
                    logger.info(
                        f"[Hybrid] Local OCR insufficient "
                        f"(cal_conf={calibrated_conf:.3f}, raw_conf={local_conf:.3f}, "
                        f"items={local_items}, catalog={catalog_match_rate:.1%}), "
                        f"proceeding to Azure..."
                    )
            except Exception as e:
                logger.warning(f"[Hybrid] Local-first pass failed: {e}")

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
                    if local_result is None:
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
        if self.azure_engine is not None:
            try:
                attempt_start = time.time()

                with optional_span(_tracer, "azure_api_call", {"azure.strategy": AZURE_MODEL_STRATEGY}) as _az_span:
                    if AZURE_MODEL_STRATEGY == "receipt-only":
                        # Use receipt model (more expensive but structured output)
                        logger.info("[Hybrid] Azure: Using receipt model (receipt-only strategy)")
                        azure_result = self.azure_engine.extract_receipt_structured(image_path)
                        azure_items = azure_result.get("items", [])
                        azure_detections = azure_result.get("ocr_detections", [])
                        model_used = "azure-receipt"
                        metadata["azure_pages_used"] = 1

                    elif AZURE_MODEL_STRATEGY == "receipt-then-read":
                        # Legacy: try receipt, fall back to read (CAN BURN 2 PAGES!)
                        logger.info("[Hybrid] Azure: receipt-then-read strategy (may use 2 pages!)")
                        azure_result = self.azure_engine.extract_receipt_structured(image_path)
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
                                read_detections = self.azure_engine.extract_text_read(image_path)
                                if read_detections:
                                    azure_detections = read_detections
                                    model_used = "azure-read"
                                    metadata["azure_pages_used"] = 2
                                    logger.warning("[Hybrid] ⚠ Used 2 Azure pages (receipt+read)")

                    else:
                        # DEFAULT: read-only — cheapest, best for handwritten ($0.0015/page)
                        logger.info("[Hybrid] Azure: Using read model (read-only strategy — most efficient)")
                        read_detections = self.azure_engine.extract_text_read(image_path)
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

                    result = {
                        "engine_used": model_used,
                        "ocr_detections": azure_detections,
                        "azure_structured": azure_result if azure_items else None,
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
                        try:
                            self.image_cache.put(cache_key, result)
                        except Exception:
                            pass
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
        if local_result is None:
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
                    if verify_detections:
                        local_result = self._cross_verify_results(
                            local_result, verify_detections
                        )
                else:
                    logger.info("[Hybrid] Cross-verify skipped (budget limit)")
            except Exception as e:
                logger.debug(f"[Hybrid] Cross-verify skipped (Azure error): {e}")

        return local_result

    def _run_azure_pipeline(
        self,
        image_path: str,
        processed_image,
        is_structured: bool,
    ) -> Dict:
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
                total_ms = int((time.time() - total_start) * 1000)
                cached_result["ocr_time_ms"] = total_ms
                cached_result["metadata"] = {"strategy": "azure-only-cached", "cache": "hit", "azure_pages_used": 0}
                logger.info(f"[Azure-Only] ✅ Cache HIT — saved Azure page ({total_ms}ms)")
                return cached_result
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
                read_detections = self.azure_engine.extract_text_read(image_path)
                if read_detections:
                    azure_detections = read_detections
                    model_used = "azure-read"
                    metadata["azure_pages_used"] = 2
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

        result = {
            "engine_used": model_used,
            "ocr_detections": azure_detections,
            "azure_structured": azure_result if azure_items else None,
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
            try:
                self.image_cache.put(cache_key, result)
            except Exception:
                pass
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
    ) -> Dict:
        """
        LOCAL mode: EasyOCR multi-pass strategy with parallel dual-pass.

        Phase 1 (gray fast) and Phase 2 (color full) run concurrently via
        ThreadPoolExecutor when OCR_PARALLEL_DUAL_PASS=True, cutting local
        scan time by ~40% for the common case where both passes are needed.

        Args:
            original_color: Pre-loaded color image (BGR).  When supplied the
                            expensive disk-read + EXIF correction is skipped.
        """
        import cv2
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from app.config import (
            OCR_SMART_PASS_THRESHOLD,
            OCR_PARALLEL_DUAL_PASS,
            IMAGE_MAX_DIMENSION,
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
        if original_color is None:
            try:
                from app.ocr.preprocessor import ImagePreprocessor as _IP
                original_color = _IP()._load_with_exif_correction(image_path)
            except Exception:
                original_color = cv2.imread(image_path)
            if original_color is not None:
                h, w = original_color.shape[:2]
                if max(h, w) > IMAGE_MAX_DIMENSION:
                    scale = IMAGE_MAX_DIMENSION / max(h, w)
                    original_color = cv2.resize(original_color, None, fx=scale, fy=scale,
                                                interpolation=cv2.INTER_AREA)

        # ── PARALLEL dual-pass: run gray fast + color full simultaneously ──
        if OCR_PARALLEL_DUAL_PASS and original_color is not None:
            logger.debug("[Local] ⚡ PARALLEL dual-pass (gray fast + color full)")
            parallel_start = time.time()

            # Two SEPARATE engine instances so threads never share internal
            # reader state — PyTorch releases the GIL during forward passes,
            # giving genuine CPU parallelism on dual-core machines.
            ocr_engine_2 = self.local_engine_2
            with ThreadPoolExecutor(max_workers=2) as executor:
                fut_gray  = executor.submit(ocr_engine.extract_text_fast, cropped_gray)
                fut_color = executor.submit(ocr_engine_2.extract_text, original_color, quality_info)
                gray_results  = fut_gray.result()
                color_results = fut_color.result()

            parallel_ms = int((time.time() - parallel_start) * 1000)
            logger.info(
                f"[Local] Parallel dual-pass done in {parallel_ms}ms "
                f"(gray={len(gray_results)}, color={len(color_results)} detections)"
            )

            # Pick the better pass; merge in the other as supplementary
            gray_conf  = self._avg_confidence(gray_results)
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
                "metadata": {**metadata, "dual_pass": "parallel"},
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

    def _check_image_quality(self, processed_image) -> Dict:
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
        from app.config import IMAGE_QUALITY_MIN_SHARPNESS, IMAGE_QUALITY_MIN_BRIGHTNESS

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

            # Sharpness: Laplacian variance (higher = sharper)
            laplacian = cv2.Laplacian(gray, cv2.CV_64F)
            result["sharpness"] = float(laplacian.var())

            # Brightness: mean pixel value
            result["brightness"] = float(np.mean(gray))

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
        self, primary: List[Dict], secondary: List[Dict]
    ) -> List[Dict]:
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
        def _det_key(det: Dict):
            text_upper = det["text"].upper().strip()
            bbox = det.get("bbox", [])
            try:
                y_mid = (float(bbox[0][1]) + float(bbox[2][1])) / 2
            except (IndexError, TypeError, ValueError):
                y_mid = 0.0
            y_bucket = round(y_mid / 60)
            return (text_upper, y_bucket)

        def _y_bucket(det: Dict):
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
        self, local_result: Dict, azure_detections: List[Dict]
    ) -> Dict:
        """
        Cross-verify local OCR results with Azure Read detections.
        Boosts confidence of items found by both engines.
        Flags items found by only one engine for review.
        """
        local_dets = local_result.get("ocr_detections", [])
        azure_texts = {d["text"].upper().strip() for d in azure_detections}

        for det in local_dets:
            local_text = det["text"].upper().strip()
            if local_text in azure_texts:
                # Both engines agree — boost confidence
                det["confidence"] = min(1.0, det["confidence"] * 1.15)
                det["needs_review"] = False
                det["cross_verified"] = True
            else:
                # Only local found this — keep but flag
                det["cross_verified"] = False

        local_result["engine_used"] = "hybrid-cross-verified"
        local_result["metadata"]["cross_verified"] = True
        return local_result

    def _quick_item_count_local(self, ocr_results: List[Dict]) -> int:
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

    def _avg_confidence(self, detections: List[Dict]) -> float:
        """Calculate average confidence across detections."""
        if not detections:
            return 0.0
        confs = [d.get("confidence", 0) for d in detections]
        return sum(confs) / len(confs)

    def _calibrated_avg_confidence(self, detections: List[Dict]) -> float:
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

    def _catalog_match_rate(self, detections: List[Dict]) -> float:
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
            return 0.0  # Empty catalog — can't verify, route to Azure

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

    def get_engine_status(self) -> Dict:
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

_hybrid_engine: Optional[HybridOCREngine] = None


def get_hybrid_engine() -> HybridOCREngine:
    """Get or create the hybrid OCR engine singleton."""
    global _hybrid_engine
    if _hybrid_engine is None:
        _hybrid_engine = HybridOCREngine()
    return _hybrid_engine
