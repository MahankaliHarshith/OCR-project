"""
Auto-Tuning Optimizer.

Searches for the optimal OCR parameter combination by running benchmarks
against training data with different parameter values.

Strategy:
    1. Grid search: exhaustive but slow — tries all combinations
    2. Smart search: starts with current defaults, perturbs one param at a time,
       keeps improvements (coordinate descent) — fast convergence

Tunable parameters:
    - OCR_CANVAS_SIZE: [960, 1280, 1600, 2048]
    - OCR_MAG_RATIO: [1.2, 1.5, 1.8, 2.2]
    - OCR_TEXT_THRESHOLD: [0.3, 0.4, 0.5, 0.6]
    - OCR_LOW_TEXT: [0.2, 0.3, 0.4]
    - IMAGE_MAX_DIMENSION: [1200, 1500, 1800, 2200]
    - GAUSSIAN_BLUR_KERNEL: [(1,1), (3,3), (5,5)]
    - CLAHE_CLIP_LIMIT: [1.5, 2.0, 2.5, 3.0]
    - FUZZY_MATCH_CUTOFF: [0.60, 0.68, 0.72, 0.78]
    - LOCAL_CONFIDENCE_SKIP_THRESHOLD: [0.75, 0.80, 0.85, 0.90]
"""

import json
import time
import logging
import itertools
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from app.training.benchmark import BenchmarkEngine

logger = logging.getLogger(__name__)


# ─── Parameter Search Space ──────────────────────────────────────────────────

DEFAULT_SEARCH_SPACE = {
    "canvas_size": [960, 1280, 1600],
    "mag_ratio": [1.2, 1.5, 1.8, 2.2],
    "text_threshold": [0.3, 0.4, 0.5],
    "low_text": [0.2, 0.3, 0.4],
    "max_dimension": [1200, 1500, 1800],
    "blur_kernel": [(1, 1), (3, 3), (5, 5)],
    "clahe_clip": [1.5, 2.0, 2.5, 3.0],
    "fuzzy_cutoff": [0.60, 0.68, 0.72, 0.78],
}

# Smaller space for quick tuning (most impactful params only)
QUICK_SEARCH_SPACE = {
    "canvas_size": [960, 1280, 1600],
    "mag_ratio": [1.5, 1.8, 2.2],
    "text_threshold": [0.3, 0.4, 0.5],
    "fuzzy_cutoff": [0.65, 0.72, 0.78],
}

# Current defaults (from config.py)
CURRENT_DEFAULTS = {
    "canvas_size": 1280,
    "mag_ratio": 1.8,
    "text_threshold": 0.4,
    "low_text": 0.3,
    "max_dimension": 1800,
    "blur_kernel": (3, 3),
    "clahe_clip": 2.0,
    "fuzzy_cutoff": 0.72,
    "confidence_skip": 0.85,
}


class Optimizer:
    """
    Auto-tunes OCR parameters for maximum accuracy on training data.
    """

    def __init__(self):
        self.benchmark = BenchmarkEngine()

    # ─── Smart Search (Coordinate Descent) ───────────────────────────────

    def smart_tune(
        self,
        samples: List[Tuple[str, Dict]],
        search_space: Optional[Dict] = None,
        metric: str = "f1_score",
        max_rounds: int = 3,
        verbose: bool = False,
    ) -> Dict:
        """
        Smart parameter tuning using coordinate descent.

        For each parameter, try all values while keeping others fixed.
        Keep the best value, then move to the next parameter.
        Repeat for max_rounds or until no improvement.

        Args:
            samples: Training (image_path, label) pairs.
            search_space: Parameter values to try (default: QUICK_SEARCH_SPACE).
            metric: Which metric to optimize (f1_score, precision, recall, etc.)
            max_rounds: Max optimization rounds.
            verbose: Include details in results.

        Returns:
            Optimization results with best parameters.
        """
        if not samples:
            return {"error": "No training samples"}

        space = search_space or QUICK_SEARCH_SPACE
        best_params = dict(CURRENT_DEFAULTS)
        history = []

        # Baseline benchmark with current defaults
        logger.info("Running baseline benchmark with current defaults...")
        baseline = self.benchmark.run_benchmark(samples, verbose=verbose)
        best_score = baseline["aggregate_metrics"].get(metric, 0.0)

        logger.info(f"Baseline {metric}: {best_score:.4f}")
        history.append({
            "round": 0,
            "type": "baseline",
            "params": dict(best_params),
            "score": best_score,
        })

        for round_num in range(1, max_rounds + 1):
            improved = False
            logger.info(f"=== Optimization Round {round_num}/{max_rounds} ===")

            for param_name, values in space.items():
                current_val = best_params.get(param_name)
                best_val = current_val
                best_param_score = best_score

                for val in values:
                    if val == current_val:
                        continue  # Skip current value

                    test_params = dict(best_params)
                    test_params[param_name] = val

                    logger.debug(f"  Testing {param_name}={val}...")
                    result = self.benchmark.run_benchmark(
                        samples, ocr_params=test_params, verbose=False
                    )
                    score = result["aggregate_metrics"].get(metric, 0.0)

                    if score > best_param_score:
                        best_param_score = score
                        best_val = val
                        logger.info(
                            f"  ✅ {param_name}={val} → {metric}={score:.4f} "
                            f"(+{score - best_score:.4f})"
                        )

                if best_val != current_val:
                    best_params[param_name] = best_val
                    best_score = best_param_score
                    improved = True
                    history.append({
                        "round": round_num,
                        "type": "improvement",
                        "param": param_name,
                        "old_value": current_val,
                        "new_value": best_val,
                        "score": best_score,
                    })

            if not improved:
                logger.info(f"No improvement in round {round_num}. Stopping early.")
                break

        # Final benchmark with best params
        logger.info("Running final benchmark with optimized params...")
        final = self.benchmark.run_benchmark(
            samples, ocr_params=best_params, verbose=verbose
        )

        return {
            "timestamp": datetime.now().isoformat(),
            "strategy": "smart_tune",
            "metric": metric,
            "total_samples": len(samples),
            "baseline_score": baseline["aggregate_metrics"].get(metric, 0.0),
            "optimized_score": final["aggregate_metrics"].get(metric, 0.0),
            "improvement": round(
                final["aggregate_metrics"].get(metric, 0.0)
                - baseline["aggregate_metrics"].get(metric, 0.0),
                4,
            ),
            "best_params": best_params,
            "baseline_metrics": baseline["aggregate_metrics"],
            "optimized_metrics": final["aggregate_metrics"],
            "optimization_history": history,
            "rounds_completed": round_num,
        }

    # ─── Grid Search (Exhaustive) ────────────────────────────────────────

    def grid_search(
        self,
        samples: List[Tuple[str, Dict]],
        search_space: Optional[Dict] = None,
        metric: str = "f1_score",
        max_combinations: int = 50,
        verbose: bool = False,
    ) -> Dict:
        """
        Exhaustive grid search over parameter combinations.

        WARNING: Can be very slow with large search spaces.
        Use max_combinations to limit runtime.

        Args:
            samples: Training (image_path, label) pairs.
            search_space: Parameter search space.
            metric: Metric to optimize.
            max_combinations: Max parameter combos to try.
            verbose: Include per-combo details.

        Returns:
            Best parameters and all results.
        """
        if not samples:
            return {"error": "No training samples"}

        space = search_space or QUICK_SEARCH_SPACE

        # Generate all combinations
        param_names = list(space.keys())
        param_values = list(space.values())
        all_combos = list(itertools.product(*param_values))

        # Limit combinations
        if len(all_combos) > max_combinations:
            logger.warning(
                f"Search space has {len(all_combos)} combinations. "
                f"Limiting to {max_combinations}."
            )
            # Sample evenly across the space
            step = len(all_combos) // max_combinations
            all_combos = all_combos[::step][:max_combinations]

        logger.info(f"Grid search: {len(all_combos)} combinations to test")
        start_time = time.time()

        best_score = -1
        best_params = None
        results_log = []

        for i, combo in enumerate(all_combos, 1):
            params = dict(zip(param_names, combo))
            logger.info(f"  [{i}/{len(all_combos)}] Testing: {params}")

            try:
                result = self.benchmark.run_benchmark(
                    samples, ocr_params=params, verbose=False
                )
                score = result["aggregate_metrics"].get(metric, 0.0)

                if verbose:
                    results_log.append({
                        "params": params,
                        "score": score,
                        "metrics": result["aggregate_metrics"],
                    })

                if score > best_score:
                    best_score = score
                    best_params = params
                    logger.info(f"    ✅ New best: {metric}={score:.4f}")

            except Exception as e:
                logger.error(f"    ❌ Failed: {e}")

        elapsed = time.time() - start_time

        return {
            "timestamp": datetime.now().isoformat(),
            "strategy": "grid_search",
            "metric": metric,
            "total_samples": len(samples),
            "combinations_tested": len(all_combos),
            "elapsed_s": round(elapsed, 2),
            "best_score": best_score,
            "best_params": best_params,
            "results_log": results_log if verbose else [],
        }

    # ─── Apply Optimized Parameters ──────────────────────────────────────

    @staticmethod
    def apply_profile(params: Dict) -> Dict:
        """
        Apply optimized parameters to the running config.

        Args:
            params: Parameter dict from optimization result.

        Returns:
            Dict of changes applied.
        """
        import app.config as cfg

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

        changes = {}
        for param_key, cfg_key in param_mapping.items():
            if param_key in params:
                old_val = getattr(cfg, cfg_key, None)
                new_val = params[param_key]
                if old_val != new_val:
                    setattr(cfg, cfg_key, new_val)
                    changes[cfg_key] = {"old": old_val, "new": new_val}
                    logger.info(f"Applied: {cfg_key} = {old_val} → {new_val}")

        return changes

    @staticmethod
    def get_current_params() -> Dict:
        """Get current OCR parameters from config."""
        import app.config as cfg

        return {
            "canvas_size": cfg.OCR_CANVAS_SIZE,
            "mag_ratio": cfg.OCR_MAG_RATIO,
            "text_threshold": cfg.OCR_TEXT_THRESHOLD,
            "low_text": cfg.OCR_LOW_TEXT,
            "link_threshold": cfg.OCR_LINK_THRESHOLD,
            "min_size": cfg.OCR_MIN_SIZE,
            "max_dimension": cfg.IMAGE_MAX_DIMENSION,
            "blur_kernel": cfg.GAUSSIAN_BLUR_KERNEL,
            "clahe_clip": cfg.CLAHE_CLIP_LIMIT,
            "fuzzy_cutoff": cfg.FUZZY_MATCH_CUTOFF,
            "confidence_skip": cfg.LOCAL_CONFIDENCE_SKIP_THRESHOLD,
        }


# Module-level singleton
optimizer = Optimizer()
