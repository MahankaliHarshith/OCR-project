"""
Real-World Trainer — Adaptive OCR Training Engine.

Closes the feedback loop between OCR scanning and accuracy improvement by
providing an interactive scan→review→correct→learn workflow.

Key capabilities:
    1. Interactive Scan & Label — run OCR, show results, accept corrections
    2. Error Pattern Mining — analyse corrections to discover OCR misreads
    3. Confusion Matrix — character-level tracking of what gets misread as what
    4. Learned Rules Generation — auto-build substitution maps from patterns
    5. Image Augmentation — generate variations for robustness testing
    6. Auto-Improve Pipeline — benchmark → analyse → learn → re-benchmark
    7. Session Tracking — record improvement history over time

Usage (from CLI — see ``scripts/trainer.py``):
    trainer scan receipt.jpg           # interactive scan & label
    trainer batch-scan ./receipts/     # bulk ingest
    trainer analyze                    # mine error patterns
    trainer learn                      # generate learned substitution rules
    trainer auto-improve               # full cycle
    trainer report                     # accuracy report
    trainer augment                    # create augmented training images
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────
TRAINING_DIR = BASE_DIR / "training_data"
LEARNED_RULES_PATH = TRAINING_DIR / "learned_rules.json"
CONFUSION_MATRIX_PATH = TRAINING_DIR / "confusion_matrix.json"
ERROR_PATTERNS_PATH = TRAINING_DIR / "error_patterns.json"
SESSION_HISTORY_PATH = TRAINING_DIR / "training_sessions.json"
AUGMENTED_DIR = TRAINING_DIR / "augmented"

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def _ensure_dirs() -> None:
    for d in (TRAINING_DIR, AUGMENTED_DIR):
        d.mkdir(parents=True, exist_ok=True)


_ensure_dirs()


# ─── Helper: Levenshtein distance (no external dep) ──────────────────────────
def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


# ─── Helper: Character-level alignment (Needleman–Wunsch) ────────────────────
def _align_strings(s1: str, s2: str) -> list[tuple[str, str]]:
    """Align two strings character-by-character using Needleman–Wunsch.

    Returns list of (char_from_s1, char_from_s2) pairs.  Gaps are ``"-"``.
    """
    gap = -1
    match_score = 2
    mismatch = -1

    n, m = len(s1), len(s2)
    score = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        score[i][0] = score[i - 1][0] + gap
    for j in range(1, m + 1):
        score[0][j] = score[0][j - 1] + gap

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sc = match_score if s1[i - 1] == s2[j - 1] else mismatch
            score[i][j] = max(
                score[i - 1][j - 1] + sc,
                score[i - 1][j] + gap,
                score[i][j - 1] + gap,
            )

    # Traceback
    pairs: list[tuple[str, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            sc = match_score if s1[i - 1] == s2[j - 1] else mismatch
            if score[i][j] == score[i - 1][j - 1] + sc:
                pairs.append((s1[i - 1], s2[j - 1]))
                i -= 1
                j -= 1
                continue
        if i > 0 and score[i][j] == score[i - 1][j] + gap:
            pairs.append((s1[i - 1], "-"))
            i -= 1
        else:
            pairs.append(("-", s2[j - 1]))
            j -= 1

    pairs.reverse()
    return pairs


# ═════════════════════════════════════════════════════════════════════════════
class RealWorldTrainer:
    """
    Adaptive OCR training engine for real-world receipt scanning.

    Wraps the full OCR pipeline and provides a feedback loop:
        scan → review → correct → mine patterns → learn rules → improve
    """

    def __init__(self) -> None:
        # Lazy-loaded heavy deps
        self._preprocessor = None
        self._hybrid_engine = None
        self._parser = None
        self._benchmark = None
        self._data_manager = None

    # ─── Lazy Properties ─────────────────────────────────────────────────

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

    @property
    def benchmark(self):
        if self._benchmark is None:
            from app.training.benchmark import BenchmarkEngine

            self._benchmark = BenchmarkEngine()
        return self._benchmark

    @property
    def data_manager(self):
        if self._data_manager is None:
            from app.training.data_manager import TrainingDataManager

            self._data_manager = TrainingDataManager()
        return self._data_manager

    def _get_parser(self):
        from app.ocr.parser import ReceiptParser
        from app.services.product_service import product_service

        catalog = product_service.get_product_code_map()
        return ReceiptParser(catalog)

    # ═════════════════════════════════════════════════════════════════════
    #  1.  INTERACTIVE SCAN & LABEL
    # ═════════════════════════════════════════════════════════════════════

    def scan_receipt(self, image_path: str) -> dict:
        """Run full OCR pipeline on one receipt and return structured results.

        This is the first step of the interactive labelling workflow.
        The caller (CLI) shows these results, lets the user correct them,
        then calls ``save_corrected_sample()`` with the corrections.

        Returns:
            {
                "image_path": str,
                "processing_ms": int,
                "raw_detections": [...],   # bounding boxes + text
                "parsed": {                # from ReceiptParser
                    "items": [{"code": ..., "quantity": ...}, ...],
                    "total_items": int,
                    "avg_confidence": float,
                },
                "receipt_id": str,
            }
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        t0 = time.time()

        # Step 1: preprocess
        processed, meta = self.preprocessor.preprocess(str(path))

        # Step 2: OCR
        hybrid_result = self.hybrid_engine.process(processed, str(path))
        detections = hybrid_result.get("detections", [])

        # Step 3: parse
        parser = self._get_parser()
        parsed = parser.parse(detections)

        elapsed_ms = int((time.time() - t0) * 1000)

        return {
            "image_path": str(path.resolve()),
            "processing_ms": elapsed_ms,
            "raw_detections": detections,
            "parsed": {
                "items": parsed.get("items", []),
                "total_items": parsed.get("total_items", 0),
                "avg_confidence": parsed.get("avg_confidence", 0.0),
            },
            "receipt_id": path.stem,
        }

    def save_corrected_sample(
        self,
        image_path: str,
        corrected_items: list[dict],
        receipt_id: str | None = None,
        receipt_type: str = "handwritten",
        notes: str = "",
        original_items: list[dict] | None = None,
    ) -> dict:
        """Save a user-corrected scan as a training sample.

        Also records individual code corrections for future error mining.

        Args:
            image_path: Path to the receipt image.
            corrected_items: List of ``{"code": "ABC", "quantity": 2}``.
            receipt_id: Optional override for sample ID.
            receipt_type: "handwritten" | "printed" | "mixed".
            notes: Free-text annotation.
            original_items: If provided, diff against corrected to log
                individual code corrections.

        Returns:
            Sample metadata from ``TrainingDataManager.add_sample()``.
        """
        total_qty = sum(item["quantity"] for item in corrected_items)

        ground_truth = {
            "items": corrected_items,
            "total_quantity": total_qty,
            "receipt_type": receipt_type,
            "notes": notes,
        }

        sample = self.data_manager.add_sample(
            image_path=image_path,
            ground_truth=ground_truth,
            receipt_id=receipt_id,
            copy_image=True,
        )

        # Record individual code corrections for error pattern mining
        if original_items:
            corrections = self._diff_items(original_items, corrected_items)
            if corrections:
                self._record_corrections(corrections, sample.get("receipt_id", ""))

        logger.info(
            f"Training sample saved: {sample.get('receipt_id')} "
            f"({len(corrected_items)} items, total_qty={total_qty})"
        )
        return sample

    # ═════════════════════════════════════════════════════════════════════
    #  2.  ERROR PATTERN MINING
    # ═════════════════════════════════════════════════════════════════════

    def mine_error_patterns(self, verbose: bool = False) -> dict:
        """Analyse all benchmark results to discover systematic OCR errors.

        Looks at:
            1. Benchmark ``missing_codes`` / ``extra_codes`` → code-level confusion
            2. Character-level alignment between misread and correct codes
            3. Correction history (if available)

        Returns:
            {
                "code_confusions": {misread: correct, ...},
                "char_confusions": {misread_char: {correct_char: count}},
                "top_error_patterns": [...],
                "total_errors_analysed": int,
            }
        """
        code_confusions: dict[str, Counter] = defaultdict(Counter)
        char_confusions: dict[str, Counter] = defaultdict(Counter)
        total_errors = 0

        # ── Source 1: Benchmark results ──
        results_dir = TRAINING_DIR / "results"
        if results_dir.exists():
            for result_file in sorted(results_dir.glob("*.json")):
                try:
                    data = json.loads(result_file.read_text(encoding="utf-8"))
                    per_image = data.get("per_image", [])
                    for img_result in per_image:
                        missing = img_result.get("missing_codes", [])
                        extras = img_result.get("extra_codes", [])
                        # Try to pair extras with missing using edit distance
                        paired = self._pair_confusions(missing, extras)
                        for correct, misread in paired:
                            code_confusions[misread][correct] += 1
                            # Character-level alignment
                            aligned = _align_strings(misread, correct)
                            for ch_from, ch_to in aligned:
                                if ch_from != ch_to and ch_from != "-" and ch_to != "-":
                                    char_confusions[ch_from][ch_to] += 1
                                    total_errors += 1
                except Exception as e:
                    logger.debug(f"Skipping result file {result_file}: {e}")

        # ── Source 2: Saved correction history ──
        corrections_file = TRAINING_DIR / "correction_log.json"
        if corrections_file.exists():
            try:
                corrections = json.loads(
                    corrections_file.read_text(encoding="utf-8")
                )
                for entry in corrections:
                    orig = entry.get("original", "").upper()
                    corrected = entry.get("corrected", "").upper()
                    if orig and corrected and orig != corrected:
                        code_confusions[orig][corrected] += 1
                        aligned = _align_strings(orig, corrected)
                        for ch_from, ch_to in aligned:
                            if ch_from != ch_to and ch_from != "-" and ch_to != "-":
                                char_confusions[ch_from][ch_to] += 1
                                total_errors += 1
            except Exception as e:
                logger.debug(f"Skipping correction log: {e}")

        # Build ranked error patterns
        top_patterns = []
        for misread, target_counts in sorted(
            code_confusions.items(), key=lambda x: sum(x[1].values()), reverse=True
        ):
            best_correct, count = target_counts.most_common(1)[0]
            top_patterns.append(
                {
                    "misread": misread,
                    "correct": best_correct,
                    "occurrences": count,
                    "edit_distance": _levenshtein(misread, best_correct),
                }
            )

        result = {
            "code_confusions": {
                k: dict(v) for k, v in code_confusions.items()
            },
            "char_confusions": {
                k: dict(v) for k, v in char_confusions.items()
            },
            "top_error_patterns": top_patterns[:50],
            "total_errors_analysed": total_errors,
        }

        # Persist
        ERROR_PATTERNS_PATH.write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        logger.info(
            f"Mined {total_errors} error instances → "
            f"{len(top_patterns)} code-level patterns"
        )

        return result

    # ═════════════════════════════════════════════════════════════════════
    #  3.  CONFUSION MATRIX
    # ═════════════════════════════════════════════════════════════════════

    def build_confusion_matrix(self) -> dict:
        """Build a character-level confusion matrix from error patterns.

        If ``mine_error_patterns()`` has not been run, runs it first.

        Returns:
            {
                "matrix": {char: {confused_as: count}},
                "most_confused": [(char, confused_as, count), ...],
                "total_confusions": int,
            }
        """
        # Use existing error patterns or mine fresh
        if ERROR_PATTERNS_PATH.exists():
            data = json.loads(
                ERROR_PATTERNS_PATH.read_text(encoding="utf-8")
            )
            char_confusions = data.get("char_confusions", {})
        else:
            data = self.mine_error_patterns()
            char_confusions = data.get("char_confusions", {})

        # Flatten to ranked list
        ranked = []
        total = 0
        for ch_from, targets in char_confusions.items():
            for ch_to, count in targets.items():
                ranked.append((ch_from, ch_to, count))
                total += count

        ranked.sort(key=lambda x: x[2], reverse=True)

        result = {
            "matrix": char_confusions,
            "most_confused": ranked[:30],
            "total_confusions": total,
        }

        CONFUSION_MATRIX_PATH.write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )

        return result

    # ═════════════════════════════════════════════════════════════════════
    #  4.  LEARNED RULES GENERATION
    # ═════════════════════════════════════════════════════════════════════

    def generate_learned_rules(
        self, min_occurrences: int = 2
    ) -> dict:
        """Auto-generate OCR substitution rules from mined error patterns.

        Rules are derived from character-level confusion data.  Only patterns
        seen ``min_occurrences`` or more times are promoted to rules to avoid
        learning from noise.

        The generated rules file is loaded by ``ReceiptParser`` at startup
        to augment the built-in ``OCR_CHAR_SUBS`` / ``HANDWRITING_SUBS``
        tables.

        Returns:
            {
                "ocr_char_rules": {char: replacement, ...},
                "reverse_rules": {char: replacement, ...},
                "code_corrections": {misread_code: correct_code, ...},
                "rules_generated": int,
                "min_occurrences": int,
            }
        """
        # Mine patterns if stale / missing
        if ERROR_PATTERNS_PATH.exists():
            data = json.loads(
                ERROR_PATTERNS_PATH.read_text(encoding="utf-8")
            )
        else:
            data = self.mine_error_patterns()

        char_confusions = data.get("char_confusions", {})
        code_confusions = data.get("code_confusions", {})

        # ── Character-level rules ──
        # Only promote if occurrences ≥ threshold and the replacement is
        # the dominant target (≥60% of confusions for that character).
        ocr_char_rules: dict[str, str] = {}
        reverse_rules: dict[str, str] = {}

        for ch_from, targets in char_confusions.items():
            total = sum(targets.values())
            if total < min_occurrences:
                continue
            best_target, best_count = max(targets.items(), key=lambda x: x[1])
            if best_count / total >= 0.6:
                # Determine direction: digit→letter or letter→digit
                if ch_from.isdigit() and best_target.isalpha():
                    ocr_char_rules[ch_from] = best_target
                elif ch_from.isalpha() and best_target.isdigit():
                    reverse_rules[ch_from] = best_target
                elif ch_from.isalpha() and best_target.isalpha():
                    # Handwriting confusion (letter→letter)
                    ocr_char_rules[ch_from] = best_target

        # ── Code-level corrections ──
        code_corrections: dict[str, str] = {}
        for misread, targets in code_confusions.items():
            total = sum(targets.values())
            if total < min_occurrences:
                continue
            best, count = max(targets.items(), key=lambda x: x[1])
            if count / total >= 0.6:
                code_corrections[misread] = best

        rules_count = len(ocr_char_rules) + len(reverse_rules) + len(code_corrections)

        result = {
            "generated_at": datetime.now().isoformat(),
            "min_occurrences": min_occurrences,
            "rules_generated": rules_count,
            "ocr_char_rules": ocr_char_rules,
            "reverse_rules": reverse_rules,
            "code_corrections": code_corrections,
        }

        LEARNED_RULES_PATH.write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )

        logger.info(
            f"Generated {rules_count} learned rules "
            f"({len(ocr_char_rules)} char, {len(reverse_rules)} reverse, "
            f"{len(code_corrections)} code) "
            f"→ {LEARNED_RULES_PATH}"
        )
        return result

    # ═════════════════════════════════════════════════════════════════════
    #  5.  IMAGE AUGMENTATION
    # ═════════════════════════════════════════════════════════════════════

    def augment_images(
        self,
        source_dir: str | None = None,
        variations: int = 3,
    ) -> dict:
        """Generate augmented copies of training images for robustness.

        Augmentations applied (randomly combined):
            - Rotation (±5°)
            - Brightness jitter (±30%)
            - Gaussian noise
            - Slight perspective warp
            - JPEG compression artefacts
            - Blur (simulating camera shake)

        Args:
            source_dir: Directory of source images.  Defaults to
                ``training_data/images``.
            variations: How many augmented copies per original image.

        Returns:
            {"augmented_count": int, "source_images": int, "output_dir": str}
        """
        src = Path(source_dir) if source_dir else TRAINING_DIR / "images"
        if not src.exists():
            return {"error": f"Source directory not found: {src}", "augmented_count": 0}

        AUGMENTED_DIR.mkdir(parents=True, exist_ok=True)
        images = [
            f for f in src.iterdir() if f.suffix.lower() in ALLOWED_IMAGE_EXTS
        ]

        if not images:
            return {"error": "No images found in source directory", "augmented_count": 0}

        count = 0
        for img_path in images:
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            for v in range(variations):
                augmented = self._apply_augmentations(img)
                out_name = f"{img_path.stem}_aug{v + 1}{img_path.suffix}"
                out_path = AUGMENTED_DIR / out_name
                cv2.imwrite(str(out_path), augmented)
                count += 1

                # Copy label if it exists
                label_src = TRAINING_DIR / "labels" / f"{img_path.stem}.json"
                if label_src.exists():
                    label_dest = TRAINING_DIR / "labels" / f"{img_path.stem}_aug{v + 1}.json"
                    if not label_dest.exists():
                        label_data = json.loads(label_src.read_text(encoding="utf-8"))
                        label_data["receipt_id"] = f"{img_path.stem}_aug{v + 1}"
                        label_data["notes"] = (
                            label_data.get("notes", "") + " [augmented]"
                        ).strip()
                        label_dest.write_text(
                            json.dumps(label_data, indent=2), encoding="utf-8"
                        )

        logger.info(
            f"Generated {count} augmented images from {len(images)} originals"
        )
        return {
            "augmented_count": count,
            "source_images": len(images),
            "output_dir": str(AUGMENTED_DIR),
        }

    # ═════════════════════════════════════════════════════════════════════
    #  6.  AUTO-IMPROVE PIPELINE
    # ═════════════════════════════════════════════════════════════════════

    def run_improvement_cycle(self, verbose: bool = False) -> dict:
        """Full automatic improvement cycle.

        Pipeline:
            1. Benchmark current accuracy (baseline)
            2. Mine error patterns from results
            3. Generate learned substitution rules
            4. Build confusion matrix
            5. Re-benchmark with learned rules loaded (new accuracy)
            6. Compare before/after → save session history

        Returns:
            {
                "baseline": {...metrics...},
                "improved": {...metrics...},
                "improvement": {
                    "f1_delta": float,
                    "code_accuracy_delta": float,
                    ...
                },
                "error_patterns_found": int,
                "rules_generated": int,
                "session_id": str,
            }
        """
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"Starting improvement cycle — session {session_id}")

        # Get training samples
        samples = self.data_manager.get_sample_pairs()
        if not samples:
            return {
                "error": "No training samples found. Add receipts first.",
                "session_id": session_id,
            }

        # ── Step 1: Baseline benchmark ──
        logger.info("Step 1/5: Running baseline benchmark...")
        baseline = self.benchmark.run_benchmark(samples, verbose=verbose)

        # Save baseline result for pattern mining
        self.data_manager.save_benchmark_result(baseline, tag="baseline")

        # ── Step 2: Mine error patterns ──
        logger.info("Step 2/5: Mining error patterns...")
        patterns = self.mine_error_patterns(verbose=verbose)

        # ── Step 3: Generate learned rules ──
        logger.info("Step 3/5: Generating learned rules...")
        rules = self.generate_learned_rules()

        # ── Step 4: Build confusion matrix ──
        logger.info("Step 4/5: Building confusion matrix...")
        matrix = self.build_confusion_matrix()

        # ── Step 5: Re-benchmark with learned rules ──
        logger.info("Step 5/5: Re-benchmarking with learned rules...")
        improved = self.benchmark.run_benchmark(samples, verbose=verbose)

        # ── Compare ──
        baseline_m = baseline.get("aggregate_metrics", {})
        improved_m = improved.get("aggregate_metrics", {})

        improvement = {}
        for key in ("f1_score", "precision", "recall", "code_accuracy", "qty_accuracy"):
            b_val = baseline_m.get(key, 0)
            i_val = improved_m.get(key, 0)
            improvement[f"{key}_delta"] = round(i_val - b_val, 4)
            improvement[f"{key}_pct_change"] = (
                round((i_val - b_val) / b_val * 100, 2) if b_val > 0 else 0
            )

        # Save session history
        session = {
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "samples_count": len(samples),
            "baseline": baseline_m,
            "improved": improved_m,
            "improvement": improvement,
            "error_patterns_found": patterns.get("total_errors_analysed", 0),
            "rules_generated": rules.get("rules_generated", 0),
            "confusion_total": matrix.get("total_confusions", 0),
        }
        self._save_session(session)

        logger.info(
            f"Improvement cycle complete — "
            f"F1: {baseline_m.get('f1_score', 0):.4f} → "
            f"{improved_m.get('f1_score', 0):.4f} "
            f"({improvement.get('f1_score_delta', 0):+.4f})"
        )

        return {
            "baseline": baseline_m,
            "improved": improved_m,
            "improvement": improvement,
            "error_patterns_found": patterns.get("total_errors_analysed", 0),
            "rules_generated": rules.get("rules_generated", 0),
            "session_id": session_id,
        }

    # ═════════════════════════════════════════════════════════════════════
    #  7.  REPORTING
    # ═════════════════════════════════════════════════════════════════════

    def generate_report(self) -> dict:
        """Generate a comprehensive training progress report.

        Aggregates data from session history, error patterns, confusion
        matrix, and current benchmark results.

        Returns:
            {
                "sessions": [...],
                "trend": {...},
                "current_accuracy": {...},
                "top_confusions": [...],
                "recommendations": [...],
            }
        """
        sessions = self._load_sessions()
        report: dict = {
            "generated_at": datetime.now().isoformat(),
            "total_sessions": len(sessions),
            "sessions": sessions[-10:],  # last 10
        }

        # Accuracy trend
        if len(sessions) >= 2:
            first = sessions[0]
            latest = sessions[-1]
            first_base = first.get("baseline", {})
            latest_imp = latest.get("improved", latest.get("baseline", {}))
            report["trend"] = {
                "first_f1": first_base.get("f1_score", 0),
                "latest_f1": latest_imp.get("f1_score", 0),
                "total_improvement": round(
                    latest_imp.get("f1_score", 0) - first_base.get("f1_score", 0), 4
                ),
                "sessions_run": len(sessions),
            }
        else:
            report["trend"] = {"note": "Need ≥2 sessions for trend data"}

        # Current learned rules
        if LEARNED_RULES_PATH.exists():
            rules = json.loads(LEARNED_RULES_PATH.read_text(encoding="utf-8"))
            report["learned_rules"] = {
                "total": rules.get("rules_generated", 0),
                "char_rules": len(rules.get("ocr_char_rules", {})),
                "reverse_rules": len(rules.get("reverse_rules", {})),
                "code_corrections": len(rules.get("code_corrections", {})),
            }
        else:
            report["learned_rules"] = {"total": 0, "note": "No rules generated yet"}

        # Top confusions
        if CONFUSION_MATRIX_PATH.exists():
            matrix = json.loads(
                CONFUSION_MATRIX_PATH.read_text(encoding="utf-8")
            )
            report["top_confusions"] = matrix.get("most_confused", [])[:10]
        else:
            report["top_confusions"] = []

        # Training data stats
        samples = self.data_manager.list_samples()
        report["training_data"] = {
            "total_samples": len(samples),
            "sample_ids": [s.get("receipt_id") for s in samples[:20]],
        }

        # Recommendations
        report["recommendations"] = self._generate_recommendations(report)

        return report

    # ═════════════════════════════════════════════════════════════════════
    #  8.  BATCH SCAN
    # ═════════════════════════════════════════════════════════════════════

    def batch_scan(self, folder_path: str) -> list[dict]:
        """Scan all receipt images in a folder (non-interactive).

        Returns list of scan results (same format as ``scan_receipt()``).
        Use this for bulk ingestion where the user reviews afterwards.
        """
        folder = Path(folder_path)
        if not folder.exists():
            raise FileNotFoundError(f"Folder not found: {folder_path}")

        images = sorted(
            f
            for f in folder.iterdir()
            if f.suffix.lower() in ALLOWED_IMAGE_EXTS
        )

        if not images:
            return []

        results = []
        for idx, img_path in enumerate(images, 1):
            logger.info(f"Scanning [{idx}/{len(images)}]: {img_path.name}")
            try:
                result = self.scan_receipt(str(img_path))
                result["scan_index"] = idx
                result["total_scans"] = len(images)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to scan {img_path.name}: {e}")
                results.append({
                    "image_path": str(img_path),
                    "error": str(e),
                    "scan_index": idx,
                    "total_scans": len(images),
                })

        return results

    # ═════════════════════════════════════════════════════════════════════
    #  INTERNAL HELPERS
    # ═════════════════════════════════════════════════════════════════════

    def _diff_items(
        self,
        original: list[dict],
        corrected: list[dict],
    ) -> list[dict]:
        """Diff original vs corrected items and return corrections."""
        corrections = []
        # Pair by position (simple — same order assumed)
        for i, (orig, corr) in enumerate(
            zip(original, corrected, strict=False)
        ):
            orig_code = orig.get("code", "").upper().strip()
            corr_code = corr.get("code", "").upper().strip()
            orig_qty = orig.get("quantity", 0)
            corr_qty = corr.get("quantity", 0)

            if orig_code != corr_code or abs(orig_qty - corr_qty) > 0.01:
                corrections.append(
                    {
                        "index": i,
                        "original": orig_code,
                        "corrected": corr_code,
                        "original_qty": orig_qty,
                        "corrected_qty": corr_qty,
                    }
                )

        # Handle extra items in corrected
        if len(corrected) > len(original):
            for i in range(len(original), len(corrected)):
                corrections.append(
                    {
                        "index": i,
                        "original": "",
                        "corrected": corrected[i].get("code", "").upper(),
                        "original_qty": 0,
                        "corrected_qty": corrected[i].get("quantity", 0),
                        "type": "missed",
                    }
                )
        return corrections

    def _record_corrections(
        self, corrections: list[dict], receipt_id: str
    ) -> None:
        """Append corrections to the persistent correction log."""
        log_path = TRAINING_DIR / "correction_log.json"
        existing = []
        if log_path.exists():
            try:
                existing = json.loads(log_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []

        for c in corrections:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "receipt_id": receipt_id,
                "original": c["original"],
                "corrected": c["corrected"],
                "original_qty": c.get("original_qty", 0),
                "corrected_qty": c.get("corrected_qty", 0),
            }
            existing.append(entry)

        log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def _pair_confusions(
        self, missing: list[str], extras: list[str]
    ) -> list[tuple[str, str]]:
        """Pair missing codes with extra codes using edit distance.

        Returns list of (correct_code, misread_code) tuples.
        Only pairs with edit distance ≤ 3 are considered plausible confusions.
        """
        if not missing or not extras:
            return []

        pairs = []
        used_extras = set()

        for correct in missing:
            best_dist = 999
            best_extra = None
            for extra in extras:
                if extra in used_extras:
                    continue
                dist = _levenshtein(correct.upper(), extra.upper())
                if dist < best_dist:
                    best_dist = dist
                    best_extra = extra

            if best_extra is not None and best_dist <= 3:
                pairs.append((correct.upper(), best_extra.upper()))
                used_extras.add(best_extra)

        return pairs

    def _apply_augmentations(self, img: np.ndarray) -> np.ndarray:
        """Apply random augmentations to an image."""
        result = img.copy()
        h, w = result.shape[:2]

        # Random rotation (±5°)
        if random.random() < 0.7:
            angle = random.uniform(-5, 5)
            center = (w // 2, h // 2)
            matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
            result = cv2.warpAffine(
                result, matrix, (w, h),
                borderMode=cv2.BORDER_REPLICATE,
            )

        # Brightness jitter (±30%)
        if random.random() < 0.7:
            factor = random.uniform(0.7, 1.3)
            result = np.clip(result * factor, 0, 255).astype(np.uint8)

        # Gaussian noise
        if random.random() < 0.5:
            noise = np.random.normal(0, random.uniform(5, 15), result.shape)
            result = np.clip(result.astype(np.float32) + noise, 0, 255).astype(
                np.uint8
            )

        # Slight blur (camera shake)
        if random.random() < 0.4:
            ksize = random.choice([3, 5])
            result = cv2.GaussianBlur(result, (ksize, ksize), 0)

        # JPEG compression artefacts
        if random.random() < 0.4:
            quality = random.randint(40, 80)
            _, encoded = cv2.imencode(
                ".jpg", result, [cv2.IMWRITE_JPEG_QUALITY, quality]
            )
            result = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

        # Slight perspective warp
        if random.random() < 0.3:
            offset = random.randint(5, 15)
            pts1 = np.float32([[0, 0], [w, 0], [0, h], [w, h]])
            pts2 = np.float32([
                [random.randint(0, offset), random.randint(0, offset)],
                [w - random.randint(0, offset), random.randint(0, offset)],
                [random.randint(0, offset), h - random.randint(0, offset)],
                [w - random.randint(0, offset), h - random.randint(0, offset)],
            ])
            matrix = cv2.getPerspectiveTransform(pts1, pts2)
            result = cv2.warpPerspective(result, matrix, (w, h))

        return result

    def _save_session(self, session: dict) -> None:
        """Append a training session to the history file."""
        sessions = self._load_sessions()
        sessions.append(session)
        SESSION_HISTORY_PATH.write_text(
            json.dumps(sessions, indent=2), encoding="utf-8"
        )

    def _load_sessions(self) -> list[dict]:
        """Load all training sessions from history."""
        if not SESSION_HISTORY_PATH.exists():
            return []
        try:
            return json.loads(SESSION_HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _generate_recommendations(self, report: dict) -> list[str]:
        """Generate actionable recommendations based on current state."""
        recs = []
        td = report.get("training_data", {})
        sample_count = td.get("total_samples", 0)

        if sample_count == 0:
            recs.append(
                "📸 Add training receipts: use `trainer scan <image>` "
                "or `trainer batch-scan <folder>` to start building your dataset."
            )
            return recs

        if sample_count < 10:
            recs.append(
                f"📊 Only {sample_count} training samples — aim for ≥20 for "
                f"reliable pattern mining.  Add more with `trainer scan`."
            )

        sessions = report.get("sessions", [])
        if not sessions:
            recs.append(
                "🔄 Run your first improvement cycle: `trainer auto-improve`"
            )

        rules = report.get("learned_rules", {})
        if rules.get("total", 0) == 0 and sample_count >= 5:
            recs.append(
                "🧠 Enough data to mine patterns — run `trainer learn` "
                "to generate OCR substitution rules."
            )

        trend = report.get("trend", {})
        if isinstance(trend.get("total_improvement"), (int, float)):
            if trend["total_improvement"] <= 0:
                recs.append(
                    "⚠️  No accuracy improvement detected yet. Try adding "
                    "more diverse receipt images (different handwriting styles, "
                    "lighting conditions)."
                )
            elif trend["total_improvement"] > 0.05:
                recs.append(
                    f"✅ Great progress! F1 improved by "
                    f"{trend['total_improvement']:.2%} across sessions."
                )

        confusions = report.get("top_confusions", [])
        if confusions:
            top = confusions[0]
            if isinstance(top, (list, tuple)) and len(top) >= 3:
                recs.append(
                    f"🔍 Top confusion: '{top[0]}' ↔ '{top[1]}' "
                    f"({top[2]} occurrences). Consider adding more receipts "
                    f"with these characters."
                )

        if sample_count >= 10 and not (TRAINING_DIR / "augmented").exists():
            recs.append(
                "🖼️  Generate augmented training images for robustness: "
                "`trainer augment`"
            )

        return recs


# ─── Module-level singleton ──────────────────────────────────────────────────
real_world_trainer = RealWorldTrainer()
