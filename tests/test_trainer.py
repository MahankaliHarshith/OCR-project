"""
Tests for Real-World Trainer utility.

Covers:
    - RealWorldTrainer core methods
    - Error pattern mining logic
    - Confusion matrix building
    - Learned rules generation
    - Image augmentation
    - Improvement cycle orchestration
    - Session tracking / reporting
    - Parser learned-rules integration
    - Helper functions (Levenshtein, Needleman–Wunsch alignment)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

# ─── Module helpers ──────────────────────────────────────────────────────────
from app.training.real_world_trainer import (
    CONFUSION_MATRIX_PATH,
    ERROR_PATTERNS_PATH,
    LEARNED_RULES_PATH,
    SESSION_HISTORY_PATH,
    TRAINING_DIR,
    RealWorldTrainer,
    _align_strings,
    _levenshtein,
)

# ═════════════════════════════════════════════════════════════════════════════
#  HELPER FUNCTION TESTS
# ═════════════════════════════════════════════════════════════════════════════


class TestLevenshtein:
    """Tests for the Levenshtein distance helper."""

    def test_identical_strings(self):
        assert _levenshtein("ABC", "ABC") == 0

    def test_empty_strings(self):
        assert _levenshtein("", "") == 0

    def test_one_empty(self):
        assert _levenshtein("ABC", "") == 3
        assert _levenshtein("", "XY") == 2

    def test_single_substitution(self):
        assert _levenshtein("ABC", "ABD") == 1

    def test_insertion(self):
        assert _levenshtein("AC", "ABC") == 1

    def test_deletion(self):
        assert _levenshtein("ABC", "AC") == 1

    def test_real_ocr_confusions(self):
        assert _levenshtein("TEW1", "TEWI") == 1  # 1↔I
        assert _levenshtein("PEPW40", "PEPW4O") == 1  # 0↔O
        assert _levenshtein("ABC", "XYZ") == 3


class TestAlignment:
    """Tests for the Needleman–Wunsch string alignment helper."""

    def test_identical(self):
        pairs = _align_strings("ABC", "ABC")
        assert pairs == [("A", "A"), ("B", "B"), ("C", "C")]

    def test_single_substitution(self):
        pairs = _align_strings("TEW1", "TEWI")
        # Should align T-T, E-E, W-W, 1-I
        chars_from = [p[0] for p in pairs]
        assert "T" in chars_from
        assert len(pairs) == 4  # no gaps needed

    def test_insertion_gap(self):
        pairs = _align_strings("AC", "ABC")
        # 'A' aligns with 'A', gap/insertion for 'B', 'C' with 'C'
        assert len(pairs) == 3

    def test_empty_strings(self):
        pairs = _align_strings("", "")
        assert pairs == []

    def test_one_empty(self):
        pairs = _align_strings("AB", "")
        assert len(pairs) == 2
        for p in pairs:
            assert p[1] == "-"


# ═════════════════════════════════════════════════════════════════════════════
#  TRAINER UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def trainer():
    """Fresh RealWorldTrainer instance."""
    return RealWorldTrainer()


@pytest.fixture
def tmp_training_dir(tmp_path):
    """Temporary training directory with test data."""
    td = tmp_path / "training_data"
    (td / "images").mkdir(parents=True)
    (td / "labels").mkdir(parents=True)
    (td / "results").mkdir(parents=True)
    (td / "profiles").mkdir(parents=True)
    return td


class TestDiffItems:
    """Tests for the item diffing logic."""

    def test_no_changes(self, trainer):
        orig = [{"code": "ABC", "quantity": 2}]
        corr = [{"code": "ABC", "quantity": 2}]
        assert trainer._diff_items(orig, corr) == []

    def test_code_change(self, trainer):
        orig = [{"code": "TEWI", "quantity": 3}]
        corr = [{"code": "TEW1", "quantity": 3}]
        diffs = trainer._diff_items(orig, corr)
        assert len(diffs) == 1
        assert diffs[0]["original"] == "TEWI"
        assert diffs[0]["corrected"] == "TEW1"

    def test_quantity_change(self, trainer):
        orig = [{"code": "ABC", "quantity": 2}]
        corr = [{"code": "ABC", "quantity": 5}]
        diffs = trainer._diff_items(orig, corr)
        assert len(diffs) == 1
        assert diffs[0]["original_qty"] == 2
        assert diffs[0]["corrected_qty"] == 5

    def test_added_item(self, trainer):
        orig = [{"code": "ABC", "quantity": 2}]
        corr = [
            {"code": "ABC", "quantity": 2},
            {"code": "XYZ", "quantity": 1},
        ]
        diffs = trainer._diff_items(orig, corr)
        assert len(diffs) == 1
        assert diffs[0]["type"] == "missed"
        assert diffs[0]["corrected"] == "XYZ"

    def test_multiple_changes(self, trainer):
        orig = [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3},
        ]
        corr = [
            {"code": "ABD", "quantity": 2},
            {"code": "DEF", "quantity": 5},
        ]
        diffs = trainer._diff_items(orig, corr)
        assert len(diffs) == 2


class TestPairConfusions:
    """Tests for the confusion pairing logic."""

    def test_empty_inputs(self, trainer):
        assert trainer._pair_confusions([], []) == []
        assert trainer._pair_confusions(["ABC"], []) == []
        assert trainer._pair_confusions([], ["ABC"]) == []

    def test_simple_pairing(self, trainer):
        missing = ["TEW1"]
        extras = ["TEWI"]
        pairs = trainer._pair_confusions(missing, extras)
        assert len(pairs) == 1
        assert pairs[0] == ("TEW1", "TEWI")

    def test_distance_threshold(self, trainer):
        # Edit distance > 3 should not pair
        missing = ["ABCDEF"]
        extras = ["XYZWVU"]
        pairs = trainer._pair_confusions(missing, extras)
        assert len(pairs) == 0

    def test_multiple_pairs(self, trainer):
        missing = ["ABC", "DEF"]
        extras = ["ABD", "DEG"]
        pairs = trainer._pair_confusions(missing, extras)
        assert len(pairs) == 2


class TestRecordCorrections:
    """Tests for correction logging."""

    def test_corrections_saved(self, trainer, tmp_path):
        log_path = TRAINING_DIR / "correction_log.json"
        # Clean up
        if log_path.exists():
            log_path.unlink()

        corrections = [
            {"original": "TEWI", "corrected": "TEW1", "original_qty": 3, "corrected_qty": 3},
        ]
        trainer._record_corrections(corrections, "test_receipt")

        assert log_path.exists()
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert len(data) >= 1
        assert data[-1]["original"] == "TEWI"
        assert data[-1]["corrected"] == "TEW1"
        assert data[-1]["receipt_id"] == "test_receipt"

        # Cleanup
        log_path.unlink(missing_ok=True)


class TestMineErrorPatterns:
    """Tests for error pattern mining."""

    def test_mine_from_benchmark_results(self, trainer, tmp_path):
        """Mine patterns from a mock benchmark result."""
        results_dir = TRAINING_DIR / "results"
        results_dir.mkdir(parents=True, exist_ok=True)

        # Create a mock benchmark result
        test_result = {
            "per_image": [
                {
                    "receipt_id": "test_001",
                    "missing_codes": ["TEW1", "PEPW40"],
                    "extra_codes": ["TEWI", "PEPW4O"],
                },
                {
                    "receipt_id": "test_002",
                    "missing_codes": ["TEW1"],
                    "extra_codes": ["TEWI"],
                },
            ]
        }
        result_file = results_dir / "test_benchmark.json"
        result_file.write_text(json.dumps(test_result), encoding="utf-8")

        try:
            patterns = trainer.mine_error_patterns()

            assert patterns["total_errors_analysed"] > 0
            assert len(patterns["top_error_patterns"]) > 0

            # TEWI should appear as a code confusion
            code_confusions = patterns["code_confusions"]
            assert "TEWI" in code_confusions
            assert "TEW1" in code_confusions["TEWI"]

            # Should be saved to disk
            assert ERROR_PATTERNS_PATH.exists()
        finally:
            result_file.unlink(missing_ok=True)
            ERROR_PATTERNS_PATH.unlink(missing_ok=True)

    def test_mine_from_correction_log(self, trainer):
        """Mine patterns from correction history."""
        log_path = TRAINING_DIR / "correction_log.json"
        log_data = [
            {"original": "TEWI", "corrected": "TEW1"},
            {"original": "TEWI", "corrected": "TEW1"},
            {"original": "PEPW4O", "corrected": "PEPW40"},
        ]
        log_path.write_text(json.dumps(log_data), encoding="utf-8")

        try:
            patterns = trainer.mine_error_patterns()
            assert patterns["total_errors_analysed"] > 0
            assert "TEWI" in patterns["code_confusions"]
        finally:
            log_path.unlink(missing_ok=True)
            ERROR_PATTERNS_PATH.unlink(missing_ok=True)

    def test_mine_empty(self, trainer):
        """Mining with no data should return empty patterns."""
        # Ensure no stale data
        ERROR_PATTERNS_PATH.unlink(missing_ok=True)
        for f in (TRAINING_DIR / "results").glob("*.json"):
            f.unlink()
        (TRAINING_DIR / "correction_log.json").unlink(missing_ok=True)

        patterns = trainer.mine_error_patterns()
        assert patterns["total_errors_analysed"] == 0


class TestConfusionMatrix:
    """Tests for confusion matrix building."""

    def test_build_from_patterns(self, trainer):
        """Build confusion matrix from existing patterns."""
        # Create error patterns
        patterns = {
            "char_confusions": {
                "I": {"1": 5},
                "O": {"0": 3},
            },
            "code_confusions": {},
        }
        ERROR_PATTERNS_PATH.write_text(
            json.dumps(patterns), encoding="utf-8"
        )

        try:
            matrix = trainer.build_confusion_matrix()
            assert matrix["total_confusions"] == 8
            assert len(matrix["most_confused"]) == 2
            # I→1 should be top confusion
            top = matrix["most_confused"][0]
            assert top[0] == "I" and top[1] == "1" and top[2] == 5
        finally:
            ERROR_PATTERNS_PATH.unlink(missing_ok=True)
            CONFUSION_MATRIX_PATH.unlink(missing_ok=True)


class TestGenerateLearnedRules:
    """Tests for learned rules generation."""

    def test_generate_rules(self, trainer):
        """Generate rules from error patterns."""
        patterns = {
            "char_confusions": {
                "I": {"1": 5, "L": 1},  # I→1 dominant (83%)
                "O": {"0": 3},          # O→0 dominant (100%)
                "X": {"Y": 1, "Z": 1},  # No dominant target — skip
            },
            "code_confusions": {
                "TEWI": {"TEW1": 4},
                "PEPW4O": {"PEPW40": 3},
            },
        }
        ERROR_PATTERNS_PATH.write_text(
            json.dumps(patterns), encoding="utf-8"
        )

        try:
            rules = trainer.generate_learned_rules(min_occurrences=2)

            assert rules["rules_generated"] > 0
            assert LEARNED_RULES_PATH.exists()

            # I→1 should be a reverse rule (letter→digit)
            assert "I" in rules["reverse_rules"]
            assert rules["reverse_rules"]["I"] == "1"

            # O→0 should be a reverse rule
            assert "O" in rules["reverse_rules"]
            assert rules["reverse_rules"]["O"] == "0"

            # TEWI→TEW1 should be a code correction
            assert "TEWI" in rules["code_corrections"]
            assert rules["code_corrections"]["TEWI"] == "TEW1"

            # X should NOT have a rule (no dominant target)
            assert "X" not in rules.get("ocr_char_rules", {})
            assert "X" not in rules.get("reverse_rules", {})
        finally:
            ERROR_PATTERNS_PATH.unlink(missing_ok=True)
            LEARNED_RULES_PATH.unlink(missing_ok=True)

    def test_min_occurrences_threshold(self, trainer):
        """Rules below min_occurrences should not be generated."""
        patterns = {
            "char_confusions": {
                "A": {"B": 1},  # only 1 occurrence — below threshold=2
            },
            "code_confusions": {},
        }
        ERROR_PATTERNS_PATH.write_text(
            json.dumps(patterns), encoding="utf-8"
        )

        try:
            rules = trainer.generate_learned_rules(min_occurrences=2)
            assert rules["rules_generated"] == 0
        finally:
            ERROR_PATTERNS_PATH.unlink(missing_ok=True)
            LEARNED_RULES_PATH.unlink(missing_ok=True)


class TestImageAugmentation:
    """Tests for image augmentation."""

    def test_augment_creates_images(self, trainer, tmp_path):
        """Augmentation should create variation images."""
        src_dir = tmp_path / "src_images"
        src_dir.mkdir()

        # Create a dummy image
        import cv2
        dummy = np.full((100, 200, 3), 128, dtype=np.uint8)
        cv2.imwrite(str(src_dir / "test.jpg"), dummy)

        result = trainer.augment_images(
            source_dir=str(src_dir), variations=2
        )

        assert result["augmented_count"] == 2
        assert result["source_images"] == 1
        assert Path(result["output_dir"]).exists()

    def test_augment_empty_dir(self, trainer, tmp_path):
        """Augmentation of empty directory should not crash."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = trainer.augment_images(source_dir=str(empty_dir))
        assert result["augmented_count"] == 0

    def test_augment_nonexistent_dir(self, trainer):
        """Augmentation of nonexistent directory should return error."""
        result = trainer.augment_images(source_dir="/nonexistent/path")
        assert "error" in result


class TestSessionTracking:
    """Tests for training session history."""

    def test_save_and_load_session(self, trainer):
        """Sessions should persist across save/load."""
        session = {
            "session_id": "test_001",
            "timestamp": "2025-01-01T00:00:00",
            "baseline": {"f1_score": 0.8},
        }

        # Clean up
        SESSION_HISTORY_PATH.unlink(missing_ok=True)

        try:
            trainer._save_session(session)
            sessions = trainer._load_sessions()
            assert len(sessions) >= 1
            assert sessions[-1]["session_id"] == "test_001"
        finally:
            SESSION_HISTORY_PATH.unlink(missing_ok=True)

    def test_load_empty_history(self, trainer):
        """Loading with no history file should return empty list."""
        SESSION_HISTORY_PATH.unlink(missing_ok=True)
        assert trainer._load_sessions() == []


class TestGenerateReport:
    """Tests for report generation."""

    def test_basic_report(self, trainer):
        """Report should generate even with no training data."""
        # Clean up to ensure fresh state
        for path in (LEARNED_RULES_PATH, CONFUSION_MATRIX_PATH, SESSION_HISTORY_PATH):
            path.unlink(missing_ok=True)

        mock_dm = MagicMock()
        mock_dm.list_samples.return_value = []
        trainer._data_manager = mock_dm
        report = trainer.generate_report()

        assert "generated_at" in report
        assert "training_data" in report
        assert "recommendations" in report
        assert report["total_sessions"] == 0

    def test_recommendations_no_data(self, trainer):
        """Should recommend adding training data when none exists."""
        SESSION_HISTORY_PATH.unlink(missing_ok=True)

        mock_dm = MagicMock()
        mock_dm.list_samples.return_value = []
        trainer._data_manager = mock_dm
        report = trainer.generate_report()

        recs = report.get("recommendations", [])
        assert len(recs) >= 1
        assert any("Add training" in r or "Add" in r for r in recs)


class TestBatchScan:
    """Tests for batch scanning."""

    def test_batch_scan_empty_folder(self, trainer, tmp_path):
        """Batch scan of empty folder should return empty list."""
        empty = tmp_path / "empty"
        empty.mkdir()
        results = trainer.batch_scan(str(empty))
        assert results == []

    def test_batch_scan_nonexistent(self, trainer):
        """Batch scan of nonexistent folder should raise."""
        with pytest.raises(FileNotFoundError):
            trainer.batch_scan("/nonexistent/path")


# ═════════════════════════════════════════════════════════════════════════════
#  PARSER INTEGRATION TESTS
# ═════════════════════════════════════════════════════════════════════════════


class TestParserLearnedRules:
    """Tests for parser integration with learned rules."""

    def test_parser_loads_learned_rules(self):
        """Parser should load learned rules from file."""
        rules = {
            "ocr_char_rules": {"X": "Y"},
            "reverse_rules": {"Z": "2"},
            "code_corrections": {"TEWI": "TEW1"},
        }
        LEARNED_RULES_PATH.write_text(json.dumps(rules), encoding="utf-8")

        try:
            from app.ocr.parser import ReceiptParser
            parser = ReceiptParser({"TEW1": "Test Product"})

            assert parser._learned_char_rules == {"X": "Y"}
            assert parser._learned_reverse_rules == {"Z": "2"}
            assert parser._learned_code_corrections == {"TEWI": "TEW1"}
        finally:
            LEARNED_RULES_PATH.unlink(missing_ok=True)

    def test_parser_works_without_rules(self):
        """Parser should work fine when no learned rules file exists."""
        LEARNED_RULES_PATH.unlink(missing_ok=True)

        from app.ocr.parser import ReceiptParser
        parser = ReceiptParser({"ABC": "Test"})

        assert parser._learned_char_rules == {}
        assert parser._learned_reverse_rules == {}
        assert parser._learned_code_corrections == {}

    def test_learned_code_correction_in_variants(self):
        """Learned code corrections should appear in generated variants."""
        rules = {
            "ocr_char_rules": {},
            "reverse_rules": {},
            "code_corrections": {"TEWI": "TEW1"},
        }
        LEARNED_RULES_PATH.write_text(json.dumps(rules), encoding="utf-8")

        try:
            from app.ocr.parser import ReceiptParser
            parser = ReceiptParser({"TEW1": "Test Product"})

            variants = parser._generate_ocr_variants("TEWI")
            assert "TEW1" in variants
        finally:
            LEARNED_RULES_PATH.unlink(missing_ok=True)

    def test_learned_char_rules_in_variants(self):
        """Learned character rules should generate additional variants."""
        rules = {
            "ocr_char_rules": {"n": "H"},
            "reverse_rules": {},
            "code_corrections": {},
        }
        LEARNED_RULES_PATH.write_text(json.dumps(rules), encoding="utf-8")

        try:
            from app.ocr.parser import ReceiptParser
            parser = ReceiptParser({"AHC": "Test"})

            variants = parser._generate_ocr_variants("AnC")
            # Should include variant with n→H
            assert "AHC" in variants
        finally:
            LEARNED_RULES_PATH.unlink(missing_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
#  AUGMENTATION INTERNALS
# ═════════════════════════════════════════════════════════════════════════════


class TestAugmentationInternals:
    """Tests for the image augmentation helper."""

    def test_augmentation_produces_valid_image(self, trainer):
        """Augmented image should be a valid numpy array."""
        img = np.full((100, 200, 3), 128, dtype=np.uint8)
        augmented = trainer._apply_augmentations(img)
        assert isinstance(augmented, np.ndarray)
        assert augmented.shape[0] > 0
        assert augmented.shape[1] > 0
        assert augmented.dtype == np.uint8

    def test_augmentation_differs_from_original(self, trainer):
        """Augmented image should not be identical to original (with high probability)."""
        img = np.full((200, 300, 3), 100, dtype=np.uint8)
        # Run augmentation multiple times — at least one should differ
        any_different = False
        for _ in range(5):
            augmented = trainer._apply_augmentations(img)
            if not np.array_equal(img, augmented):
                any_different = True
                break
        assert any_different, "Augmentation should produce visible changes"
