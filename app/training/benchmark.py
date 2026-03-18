"""
Benchmark / Evaluation Engine.

Runs the OCR pipeline against labeled training data and computes accuracy
metrics at item level, code level, and quantity level.

Metrics produced:
    - Item precision:  correct items found / total items found
    - Item recall:     correct items found / total expected items
    - Item F1:         harmonic mean of precision and recall
    - Code accuracy:   % of expected codes correctly detected
    - Quantity accuracy: % of detected items with correct quantity
    - Total qty accuracy: % of receipts where total quantity matches
    - Average confidence: mean OCR confidence across all detections
"""

import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class BenchmarkEngine:
    """
    Evaluates OCR pipeline accuracy against ground-truth labeled data.
    """

    def __init__(self):
        # Lazy-load heavy dependencies
        self._preprocessor = None
        self._hybrid_engine = None
        self._parser = None

    @property
    def preprocessor(self):
        if self._preprocessor is None:
            from app.ocr.preprocessor import ImagePreprocessor
            self._preprocessor = ImagePreprocessor()
        return self._preprocessor

    @property
    def hybrid_engine(self):
        if self._hybrid_engine is None:
            from app.ocr.hybrid_engine import get_hybrid_engine
            self._hybrid_engine = get_hybrid_engine()
        return self._hybrid_engine

    def _get_parser(self):
        """Get a fresh parser with current catalog."""
        from app.ocr.parser import ReceiptParser
        from app.services.product_service import product_service
        catalog = product_service.get_product_code_map()
        return ReceiptParser(catalog)

    # ─── Core Benchmark ──────────────────────────────────────────────────

    def run_benchmark(
        self,
        samples: list[tuple[str, dict]],
        ocr_params: dict | None = None,
        verbose: bool = False,
    ) -> dict:
        """
        Run OCR pipeline on all labeled samples and compute accuracy.

        Args:
            samples: List of (image_path, label_dict) pairs.
            ocr_params: Optional OCR parameter overrides for testing.
            verbose: If True, include per-image detail in results.

        Returns:
            Comprehensive benchmark results dict.
        """
        if not samples:
            return {"error": "No training samples to benchmark", "total_samples": 0}

        start_time = time.time()
        parser = self._get_parser()

        per_image = []
        total_expected_items = 0
        total_detected_items = 0
        total_correct_items = 0
        total_correct_qty = 0
        total_correct_total_qty = 0
        total_codes_expected = 0
        total_codes_found = 0
        all_confidences = []
        total_time_ms = 0

        for idx, (image_path, label) in enumerate(samples, 1):
            logger.info(
                f"Benchmarking [{idx}/{len(samples)}]: {label.get('receipt_id', 'unknown')}"
            )

            try:
                result = self._process_single(
                    image_path, label, parser, ocr_params
                )
            except Exception as e:
                logger.error(f"Failed to process {image_path}: {e}")
                result = {
                    "receipt_id": label.get("receipt_id", "unknown"),
                    "error": str(e),
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1": 0.0,
                    "code_accuracy": 0.0,
                    "qty_accuracy": 0.0,
                    "total_qty_match": False,
                    "processing_ms": 0,
                    "avg_confidence": 0.0,
                }

            per_image.append(result)

            # Accumulate stats
            expected = label.get("items", [])
            total_expected_items += len(expected)
            total_detected_items += result.get("detected_count", 0)
            total_correct_items += result.get("correct_items", 0)
            total_correct_qty += result.get("correct_qty_count", 0)
            total_codes_expected += len(expected)
            total_codes_found += result.get("codes_found", 0)
            if result.get("total_qty_match"):
                total_correct_total_qty += 1
            if result.get("avg_confidence", 0) > 0:
                all_confidences.append(result["avg_confidence"])
            total_time_ms += result.get("processing_ms", 0)

        elapsed = time.time() - start_time

        # Compute aggregate metrics
        precision = (
            total_correct_items / total_detected_items
            if total_detected_items > 0
            else 0.0
        )
        recall = (
            total_correct_items / total_expected_items
            if total_expected_items > 0
            else 0.0
        )
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        code_accuracy = (
            total_codes_found / total_codes_expected
            if total_codes_expected > 0
            else 0.0
        )
        qty_accuracy = (
            total_correct_qty / total_correct_items
            if total_correct_items > 0
            else 0.0
        )
        total_qty_accuracy = (
            total_correct_total_qty / len(samples) if samples else 0.0
        )
        avg_confidence = (
            sum(all_confidences) / len(all_confidences) if all_confidences else 0.0
        )

        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_samples": len(samples),
            "total_elapsed_s": round(elapsed, 2),
            "avg_time_per_receipt_ms": round(total_time_ms / len(samples), 0) if samples else 0,
            "aggregate_metrics": {
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1_score": round(f1, 4),
                "code_accuracy": round(code_accuracy, 4),
                "qty_accuracy": round(qty_accuracy, 4),
                "total_qty_accuracy": round(total_qty_accuracy, 4),
                "avg_confidence": round(avg_confidence, 4),
            },
            "totals": {
                "expected_items": total_expected_items,
                "detected_items": total_detected_items,
                "correct_items": total_correct_items,
                "correct_quantities": total_correct_qty,
                "correct_total_qty": total_correct_total_qty,
            },
            "ocr_params": ocr_params or "default",
        }

        if verbose:
            summary["per_image"] = per_image

        return summary

    def _process_single(
        self,
        image_path: str,
        label: dict,
        parser,
        ocr_params: dict | None = None,
    ) -> dict:
        """
        Process one image and compare against ground truth.

        Returns per-image metrics dict.
        """
        receipt_id = label.get("receipt_id", "unknown")
        expected_items = label.get("items", [])
        expected_total_qty = label.get(
            "total_quantity",
            sum(item["quantity"] for item in expected_items),
        )

        t0 = time.time()

        # ── Step 1: Preprocess ──
        processed_image, preprocess_meta = self.preprocessor.preprocess(image_path)

        # ── Step 2: OCR ──
        # If custom params provided, apply them temporarily
        if ocr_params:
            detections = self._ocr_with_params(processed_image, image_path, ocr_params)
        else:
            hybrid_result = self.hybrid_engine.process(
                processed_image, image_path
            )
            detections = hybrid_result.get("detections", [])

        processing_ms = int((time.time() - t0) * 1000)

        # ── Step 3: Parse ──
        parse_result = parser.parse(detections)
        detected_items = parse_result.get("items", [])

        # ── Step 4: Compare against ground truth ──
        comparison = self._compare_items(expected_items, detected_items)

        avg_conf = parse_result.get("avg_confidence", 0.0)

        return {
            "receipt_id": receipt_id,
            "processing_ms": processing_ms,
            "avg_confidence": avg_conf,
            "expected_count": len(expected_items),
            "detected_count": len(detected_items),
            "correct_items": comparison["correct_items"],
            "correct_qty_count": comparison["correct_qty"],
            "codes_found": comparison["codes_found"],
            "precision": comparison["precision"],
            "recall": comparison["recall"],
            "f1": comparison["f1"],
            "code_accuracy": comparison["code_accuracy"],
            "qty_accuracy": comparison["qty_accuracy"],
            "total_qty_match": (
                parse_result.get("total_items", 0) == expected_total_qty
            ),
            "detected_total_qty": parse_result.get("total_items", 0),
            "expected_total_qty": expected_total_qty,
            "missing_codes": comparison["missing_codes"],
            "extra_codes": comparison["extra_codes"],
            "qty_mismatches": comparison["qty_mismatches"],
        }

    def _compare_items(
        self,
        expected: list[dict],
        detected: list[dict],
    ) -> dict:
        """
        Compare expected items against detected items.

        Matching is case-insensitive on product code.
        """
        # Build expected map: code → total quantity
        expected_map = {}
        for item in expected:
            code = item["code"].upper().strip()
            expected_map[code] = expected_map.get(code, 0) + item["quantity"]

        # Build detected map: code → total quantity
        detected_map = {}
        for item in detected:
            code = item.get("code", "").upper().strip()
            if code:
                detected_map[code] = detected_map.get(code, 0) + item.get("quantity", 0)

        # Count matches
        correct_items = 0  # code found (regardless of qty)
        correct_qty = 0  # code found AND quantity matches
        codes_found = 0

        missing_codes = []
        qty_mismatches = []

        for code, exp_qty in expected_map.items():
            if code in detected_map:
                codes_found += 1
                correct_items += 1
                det_qty = detected_map[code]
                if abs(det_qty - exp_qty) < 0.01:  # float tolerance
                    correct_qty += 1
                else:
                    qty_mismatches.append({
                        "code": code,
                        "expected": exp_qty,
                        "detected": det_qty,
                    })
            else:
                missing_codes.append(code)

        extra_codes = [
            code for code in detected_map if code not in expected_map
        ]

        total_expected = len(expected_map)
        total_detected = len(detected_map)

        precision = correct_items / total_detected if total_detected > 0 else 0.0
        recall = correct_items / total_expected if total_expected > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        code_accuracy = codes_found / total_expected if total_expected > 0 else 0.0
        qty_accuracy = correct_qty / correct_items if correct_items > 0 else 0.0

        return {
            "correct_items": correct_items,
            "correct_qty": correct_qty,
            "codes_found": codes_found,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "code_accuracy": round(code_accuracy, 4),
            "qty_accuracy": round(qty_accuracy, 4),
            "missing_codes": missing_codes,
            "extra_codes": extra_codes,
            "qty_mismatches": qty_mismatches,
        }

    def _ocr_with_params(
        self,
        processed_image,
        image_path: str,
        params: dict,
    ) -> list[dict]:
        """
        Run OCR with custom parameter overrides (for auto-tuning).

        Temporarily patches config values, runs OCR, then restores.
        """
        import app.config as cfg

        # Save originals
        originals = {}
        param_mapping = {
            "canvas_size": "OCR_CANVAS_SIZE",
            "mag_ratio": "OCR_MAG_RATIO",
            "text_threshold": "OCR_TEXT_THRESHOLD",
            "low_text": "OCR_LOW_TEXT",
            "link_threshold": "OCR_LINK_THRESHOLD",
            "min_size": "OCR_MIN_SIZE",
            "max_dimension": "IMAGE_MAX_DIMENSION",
            "blur_kernel": "GAUSSIAN_BLUR_KERNEL",
            "clahe_clip": "CLAHE_CLIP_LIMIT",
            "fuzzy_cutoff": "FUZZY_MATCH_CUTOFF",
            "confidence_skip": "LOCAL_CONFIDENCE_SKIP_THRESHOLD",
        }

        for param_key, cfg_key in param_mapping.items():
            if param_key in params:
                originals[cfg_key] = getattr(cfg, cfg_key)
                setattr(cfg, cfg_key, params[param_key])

        try:
            hybrid_result = self.hybrid_engine.process(
                processed_image, image_path
            )
            return hybrid_result.get("detections", [])
        finally:
            # Restore originals
            for cfg_key, orig_val in originals.items():
                setattr(cfg, cfg_key, orig_val)

    # ─── Quick Benchmark (single image) ──────────────────────────────────

    def benchmark_single(
        self,
        image_path: str,
        ground_truth: dict,
        ocr_params: dict | None = None,
    ) -> dict:
        """Benchmark a single image against its ground truth."""
        label = {
            "receipt_id": "single_test",
            "items": ground_truth["items"],
            "total_quantity": ground_truth.get(
                "total_quantity",
                sum(i["quantity"] for i in ground_truth["items"]),
            ),
        }
        return self.run_benchmark(
            [(image_path, label)],
            ocr_params=ocr_params,
            verbose=True,
        )


# Module-level singleton
benchmark_engine = BenchmarkEngine()
