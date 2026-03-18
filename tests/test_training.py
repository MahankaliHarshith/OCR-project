"""
Tests for the Training Pipeline.

Covers:
    - Data manager: add, list, query, delete, validation
    - Benchmark engine: item comparison logic, metric computation
    - Optimizer: parameter management, profile application
    - Template learner: template creation and serialization
    - API routes: upload, benchmark, optimize endpoints
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_training_dir(tmp_path):
    """Create a temporary training data directory."""
    images_dir = tmp_path / "images"
    labels_dir = tmp_path / "labels"
    results_dir = tmp_path / "results"
    profiles_dir = tmp_path / "profiles"
    for d in (images_dir, labels_dir, results_dir, profiles_dir):
        d.mkdir()
    return tmp_path


@pytest.fixture
def sample_image(tmp_path):
    """Create a minimal valid JPEG-like image file for testing."""
    import cv2
    img = np.full((200, 300, 3), 220, dtype=np.uint8)
    # Add some text-like markings
    cv2.putText(img, "ABC 2", (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    cv2.putText(img, "DEF 3", (50, 130), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    path = tmp_path / "test_receipt.jpg"
    cv2.imwrite(str(path), img)
    return str(path)


@pytest.fixture
def sample_ground_truth():
    """Valid ground truth for testing."""
    return {
        "items": [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3},
        ],
        "total_quantity": 5,
        "receipt_type": "handwritten",
        "notes": "Test receipt",
    }


@pytest.fixture
def data_manager(temp_training_dir):
    """Create a DataManager pointing to temp directory."""
    import app.training.data_manager as dm_mod
    from app.training.data_manager import TrainingDataManager

    # Patch directory constants
    orig_images = dm_mod.IMAGES_DIR
    orig_labels = dm_mod.LABELS_DIR
    orig_results = dm_mod.RESULTS_DIR
    orig_profiles = dm_mod.PROFILES_DIR

    dm_mod.IMAGES_DIR = temp_training_dir / "images"
    dm_mod.LABELS_DIR = temp_training_dir / "labels"
    dm_mod.RESULTS_DIR = temp_training_dir / "results"
    dm_mod.PROFILES_DIR = temp_training_dir / "profiles"

    manager = TrainingDataManager()

    yield manager

    # Restore
    dm_mod.IMAGES_DIR = orig_images
    dm_mod.LABELS_DIR = orig_labels
    dm_mod.RESULTS_DIR = orig_results
    dm_mod.PROFILES_DIR = orig_profiles


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MANAGER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataManager:
    """Tests for TrainingDataManager."""

    def test_add_sample(self, data_manager, sample_image, sample_ground_truth):
        """Adding a sample copies image and creates label file."""
        result = data_manager.add_sample(sample_image, sample_ground_truth)

        assert result["receipt_id"] == "test_receipt"
        assert len(result["items"]) == 2
        assert result["total_quantity"] == 5
        assert result["receipt_type"] == "handwritten"

    def test_add_sample_auto_total(self, data_manager, sample_image):
        """Total quantity auto-computed from items if not provided."""
        gt = {"items": [{"code": "A", "quantity": 3}, {"code": "B", "quantity": 7}]}
        result = data_manager.add_sample(sample_image, gt)
        assert result["total_quantity"] == 10

    def test_add_sample_custom_id(self, data_manager, sample_image, sample_ground_truth):
        """Custom receipt ID is used."""
        result = data_manager.add_sample(
            sample_image, sample_ground_truth, receipt_id="custom_001"
        )
        assert result["receipt_id"] == "custom_001"

    def test_add_sample_unique_id(self, data_manager, sample_image, sample_ground_truth):
        """Duplicate IDs get a suffix."""
        r1 = data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="dup")
        r2 = data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="dup")
        assert r1["receipt_id"] == "dup"
        assert r2["receipt_id"] == "dup_2"

    def test_add_sample_invalid_no_items(self, data_manager, sample_image):
        """Reject ground truth without items."""
        with pytest.raises(ValueError, match="items"):
            data_manager.add_sample(sample_image, {"no_items": []})

    def test_add_sample_invalid_empty_items(self, data_manager, sample_image):
        """Reject ground truth with empty items list."""
        with pytest.raises(ValueError, match="empty"):
            data_manager.add_sample(sample_image, {"items": []})

    def test_add_sample_invalid_missing_code(self, data_manager, sample_image):
        """Reject item without code."""
        with pytest.raises(ValueError, match="code"):
            data_manager.add_sample(
                sample_image, {"items": [{"quantity": 2}]}
            )

    def test_add_sample_invalid_missing_qty(self, data_manager, sample_image):
        """Reject item without quantity."""
        with pytest.raises(ValueError, match="quantity"):
            data_manager.add_sample(
                sample_image, {"items": [{"code": "ABC"}]}
            )

    def test_add_sample_invalid_zero_qty(self, data_manager, sample_image):
        """Reject item with zero quantity."""
        with pytest.raises(ValueError, match="positive"):
            data_manager.add_sample(
                sample_image, {"items": [{"code": "ABC", "quantity": 0}]}
            )

    def test_add_sample_invalid_image_not_found(self, data_manager, sample_ground_truth):
        """Reject non-existent image."""
        with pytest.raises(ValueError, match="not found"):
            data_manager.add_sample("/fake/path.jpg", sample_ground_truth)

    def test_add_sample_invalid_extension(self, data_manager, sample_ground_truth, tmp_path):
        """Reject unsupported image format."""
        bad_file = tmp_path / "receipt.pdf"
        bad_file.write_text("fake")
        with pytest.raises(ValueError, match="Unsupported"):
            data_manager.add_sample(str(bad_file), sample_ground_truth)

    def test_list_samples(self, data_manager, sample_image, sample_ground_truth):
        """List returns all added samples."""
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="r1")
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="r2")
        samples = data_manager.list_samples()
        assert len(samples) == 2
        ids = {s["receipt_id"] for s in samples}
        assert ids == {"r1", "r2"}

    def test_get_sample(self, data_manager, sample_image, sample_ground_truth):
        """Get a specific sample by ID."""
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="abc")
        sample = data_manager.get_sample("abc")
        assert sample is not None
        assert sample["receipt_id"] == "abc"

    def test_get_sample_not_found(self, data_manager):
        """Get returns None for unknown ID."""
        assert data_manager.get_sample("nonexistent") is None

    def test_get_sample_pairs(self, data_manager, sample_image, sample_ground_truth):
        """Get valid image-label pairs for benchmarking."""
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="pair1")
        pairs = data_manager.get_sample_pairs()
        assert len(pairs) == 1
        assert pairs[0][1]["receipt_id"] == "pair1"

    def test_count_samples(self, data_manager, sample_image, sample_ground_truth):
        """Count returns correct number."""
        assert data_manager.count_samples() == 0
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="c1")
        assert data_manager.count_samples() == 1
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="c2")
        assert data_manager.count_samples() == 2

    def test_delete_sample(self, data_manager, sample_image, sample_ground_truth):
        """Delete removes image and label."""
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="del1")
        assert data_manager.count_samples() == 1
        assert data_manager.delete_sample("del1") is True
        assert data_manager.count_samples() == 0

    def test_delete_sample_not_found(self, data_manager):
        """Delete returns False for unknown ID."""
        assert data_manager.delete_sample("nope") is False

    def test_clear_all(self, data_manager, sample_image, sample_ground_truth):
        """Clear removes all samples."""
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="x1")
        data_manager.add_sample(sample_image, sample_ground_truth, receipt_id="x2")
        count = data_manager.clear_all()
        assert count == 2
        assert data_manager.count_samples() == 0

    def test_add_sample_from_bytes(self, data_manager, sample_image, sample_ground_truth):
        """Add sample from raw bytes."""
        image_bytes = Path(sample_image).read_bytes()
        result = data_manager.add_sample_from_bytes(
            image_bytes, "upload.jpg", sample_ground_truth
        )
        assert result["receipt_id"] == "upload"
        assert len(result["items"]) == 2

    def test_save_and_load_profile(self, data_manager):
        """Save and load optimization profiles."""
        profile = {"params": {"canvas_size": 1280}, "score": 0.95}
        data_manager.save_profile(profile, "test_profile")
        loaded = data_manager.load_profile("test_profile")
        assert loaded is not None
        assert loaded["score"] == 0.95

    def test_load_profile_not_found(self, data_manager):
        """Load returns None for missing profile."""
        assert data_manager.load_profile("ghost") is None

    def test_save_benchmark_result(self, data_manager):
        """Save and list benchmark results."""
        result = {"f1": 0.85, "precision": 0.9}
        path = data_manager.save_benchmark_result(result)
        assert Path(path).exists()

        results = data_manager.list_benchmark_results()
        assert len(results) == 1
        assert results[0]["f1"] == 0.85


# ═══════════════════════════════════════════════════════════════════════════════
# BENCHMARK ENGINE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestBenchmarkEngine:
    """Tests for the benchmark comparison logic."""

    def test_compare_items_perfect_match(self):
        """All items found with correct quantities → perfect score."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3},
        ]
        detected = [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3},
        ]

        result = engine._compare_items(expected, detected)
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["f1"] == 1.0
        assert result["code_accuracy"] == 1.0
        assert result["qty_accuracy"] == 1.0
        assert result["missing_codes"] == []
        assert result["extra_codes"] == []

    def test_compare_items_partial_match(self):
        """Some items found, some missing."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3},
            {"code": "GHI", "quantity": 1},
        ]
        detected = [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3},
        ]

        result = engine._compare_items(expected, detected)
        assert result["recall"] == pytest.approx(2 / 3, abs=0.01)
        assert result["precision"] == 1.0  # All detected were correct
        assert "GHI" in result["missing_codes"]

    def test_compare_items_wrong_qty(self):
        """Code found but wrong quantity."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [{"code": "ABC", "quantity": 5}]
        detected = [{"code": "ABC", "quantity": 3}]

        result = engine._compare_items(expected, detected)
        assert result["code_accuracy"] == 1.0  # Code was found
        assert result["qty_accuracy"] == 0.0  # But qty wrong
        assert len(result["qty_mismatches"]) == 1
        assert result["qty_mismatches"][0]["expected"] == 5
        assert result["qty_mismatches"][0]["detected"] == 3

    def test_compare_items_extra_codes(self):
        """Detected items not in expected (false positives)."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [{"code": "ABC", "quantity": 2}]
        detected = [
            {"code": "ABC", "quantity": 2},
            {"code": "XYZ", "quantity": 1},
        ]

        result = engine._compare_items(expected, detected)
        assert result["precision"] == pytest.approx(0.5, abs=0.01)
        assert result["recall"] == 1.0
        assert "XYZ" in result["extra_codes"]

    def test_compare_items_no_detections(self):
        """No detections at all."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [{"code": "ABC", "quantity": 2}]
        detected = []

        result = engine._compare_items(expected, detected)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["f1"] == 0.0

    def test_compare_items_case_insensitive(self):
        """Code matching is case-insensitive."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [{"code": "abc", "quantity": 2}]
        detected = [{"code": "ABC", "quantity": 2}]

        result = engine._compare_items(expected, detected)
        assert result["code_accuracy"] == 1.0

    def test_compare_items_empty_expected(self):
        """No expected items."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        result = engine._compare_items([], [{"code": "A", "quantity": 1}])
        assert result["recall"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIMIZER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptimizer:
    """Tests for the parameter optimizer."""

    def test_get_current_params(self):
        """Returns a dict of current OCR parameters."""
        from app.training.optimizer import Optimizer
        opt = Optimizer()
        params = opt.get_current_params()

        assert "canvas_size" in params
        assert "mag_ratio" in params
        assert "text_threshold" in params
        assert "fuzzy_cutoff" in params
        assert isinstance(params["canvas_size"], int)
        assert isinstance(params["mag_ratio"], float)

    def test_apply_profile_changes_config(self):
        """Applying a profile updates config values."""
        import app.config as cfg
        from app.training.optimizer import Optimizer

        opt = Optimizer()
        original_cutoff = cfg.FUZZY_MATCH_CUTOFF

        try:
            changes = opt.apply_profile({"fuzzy_cutoff": 0.99})
            assert cfg.FUZZY_MATCH_CUTOFF == 0.99
            assert "FUZZY_MATCH_CUTOFF" in changes
            assert changes["FUZZY_MATCH_CUTOFF"]["old"] == original_cutoff
            assert changes["FUZZY_MATCH_CUTOFF"]["new"] == 0.99
        finally:
            cfg.FUZZY_MATCH_CUTOFF = original_cutoff

    def test_apply_profile_no_changes(self):
        """No changes when values are already optimal."""
        import app.config as cfg
        from app.training.optimizer import Optimizer

        opt = Optimizer()
        current = cfg.FUZZY_MATCH_CUTOFF
        changes = opt.apply_profile({"fuzzy_cutoff": current})
        assert changes == {}  # No change needed

    def test_smart_tune_no_samples(self):
        """Smart tune returns error with no samples."""
        from app.training.optimizer import Optimizer
        opt = Optimizer()
        result = opt.smart_tune([])
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# TEMPLATE LEARNER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplateLearner:
    """Tests for the template learning system."""

    def test_template_serialization(self):
        """ReceiptTemplate round-trips through JSON."""
        from app.training.template_learner import ReceiptTemplate

        t = ReceiptTemplate("test")
        t.samples_used = 5
        t.code_column_x = 0.15
        t.qty_column_x = 0.75
        t.is_structured = True
        t.has_prices = True
        t.avg_line_height = 0.04

        data = t.to_dict()
        t2 = ReceiptTemplate.from_dict(data)

        assert t2.template_id == "test"
        assert t2.samples_used == 5
        assert t2.code_column_x == pytest.approx(0.15, abs=0.001)
        assert t2.qty_column_x == pytest.approx(0.75, abs=0.001)
        assert t2.is_structured is True
        assert t2.has_prices is True

    def test_template_save_load(self, tmp_path):
        """Save and load template from disk."""
        from app.training.template_learner import ReceiptTemplate, TemplateLearner

        t = ReceiptTemplate("mytemplate")
        t.code_column_x = 0.2
        t.avg_detections = 15

        TemplateLearner.save_template(t, directory=str(tmp_path))
        loaded = TemplateLearner.load_template("mytemplate", directory=str(tmp_path))

        assert loaded is not None
        assert loaded.template_id == "mytemplate"
        assert loaded.code_column_x == pytest.approx(0.2, abs=0.001)
        assert loaded.avg_detections == 15

    def test_template_load_not_found(self, tmp_path):
        """Load returns None for missing template."""
        from app.training.template_learner import TemplateLearner
        assert TemplateLearner.load_template("nope", str(tmp_path)) is None

    def test_list_templates(self, tmp_path):
        """List returns saved template IDs."""
        from app.training.template_learner import ReceiptTemplate, TemplateLearner

        t1 = ReceiptTemplate("alpha")
        t2 = ReceiptTemplate("beta")
        TemplateLearner.save_template(t1, str(tmp_path))
        TemplateLearner.save_template(t2, str(tmp_path))

        templates = TemplateLearner.list_templates(str(tmp_path))
        assert "alpha" in templates
        assert "beta" in templates

    def test_template_preprocessing_hints(self):
        """Template provides preprocessing hints based on structure."""
        from app.training.template_learner import ReceiptTemplate

        t = ReceiptTemplate("structured")
        t.is_structured = True

        # Structured receipts should get different preprocessing
        data = t.to_dict()
        assert "preprocessing_hints" in data

    def test_learn_template_no_samples(self):
        """Learn raises ValueError with no samples."""
        from app.training.template_learner import TemplateLearner
        learner = TemplateLearner()
        with pytest.raises(ValueError, match="No samples"):
            learner.learn_template([], "empty")


# ═══════════════════════════════════════════════════════════════════════════════
# GROUND TRUTH VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroundTruthValidation:
    """Edge cases for ground truth validation."""

    def test_negative_quantity(self, data_manager, sample_image):
        """Reject negative quantity."""
        with pytest.raises(ValueError, match="positive"):
            data_manager.add_sample(
                sample_image, {"items": [{"code": "X", "quantity": -1}]}
            )

    def test_non_string_code(self, data_manager, sample_image):
        """Reject non-string code."""
        with pytest.raises(ValueError, match="non-empty string"):
            data_manager.add_sample(
                sample_image, {"items": [{"code": 123, "quantity": 1}]}
            )

    def test_empty_code(self, data_manager, sample_image):
        """Reject empty code."""
        with pytest.raises(ValueError, match="non-empty string"):
            data_manager.add_sample(
                sample_image, {"items": [{"code": "", "quantity": 1}]}
            )

    def test_items_not_list(self, data_manager, sample_image):
        """Reject items that aren't a list."""
        with pytest.raises(ValueError, match="list"):
            data_manager.add_sample(
                sample_image, {"items": "not_a_list"}
            )

    def test_float_quantity(self, data_manager, sample_image, sample_ground_truth):
        """Accept float quantities (e.g., 2.5 liters)."""
        gt = {"items": [{"code": "LTR", "quantity": 2.5}]}
        result = data_manager.add_sample(sample_image, gt, receipt_id="float_test")
        assert result["items"][0]["quantity"] == 2.5


# ═══════════════════════════════════════════════════════════════════════════════
# BENCHMARK METRICS EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestBenchmarkMetrics:
    """Edge cases for benchmark metric computation."""

    def test_duplicate_codes_aggregated(self):
        """Multiple items with same code are aggregated."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        expected = [
            {"code": "ABC", "quantity": 2},
            {"code": "ABC", "quantity": 3},
        ]
        detected = [{"code": "ABC", "quantity": 5}]

        result = engine._compare_items(expected, detected)
        assert result["code_accuracy"] == 1.0
        assert result["qty_accuracy"] == 1.0

    def test_f1_calculation(self):
        """F1 is harmonic mean of precision and recall."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()

        # 2 expected, 2 detected, 1 correct
        expected = [{"code": "A", "quantity": 1}, {"code": "B", "quantity": 1}]
        detected = [{"code": "A", "quantity": 1}, {"code": "C", "quantity": 1}]

        result = engine._compare_items(expected, detected)
        # Precision = 1/2 = 0.5, Recall = 1/2 = 0.5
        assert result["f1"] == pytest.approx(0.5, abs=0.01)

    def test_benchmark_empty_samples(self):
        """Benchmark with empty samples returns error."""
        from app.training.benchmark import BenchmarkEngine
        engine = BenchmarkEngine()
        result = engine.run_benchmark([])
        assert result["total_samples"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
