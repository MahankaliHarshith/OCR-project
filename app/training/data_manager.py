"""
Training Data Manager.

Handles ingestion, storage, and retrieval of labeled receipt images.
Each training sample = receipt image + ground truth JSON.

Directory layout:
    training_data/
        images/          ← receipt image files (jpg, png, etc.)
        labels/          ← ground truth JSON files (one per image)
        results/         ← benchmark run results
        profiles/        ← optimized parameter profiles

Ground truth JSON schema:
    {
        "receipt_id": "receipt_001",
        "items": [
            {"code": "ABC", "quantity": 2},
            {"code": "DEF", "quantity": 3}
        ],
        "total_quantity": 5,
        "receipt_type": "handwritten",   // optional
        "notes": "Slightly blurry"       // optional
    }
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

# ─── Paths ───────────────────────────────────────────────────────────────────
TRAINING_DIR = BASE_DIR / "training_data"
IMAGES_DIR = TRAINING_DIR / "images"
LABELS_DIR = TRAINING_DIR / "labels"
RESULTS_DIR = TRAINING_DIR / "results"
PROFILES_DIR = TRAINING_DIR / "profiles"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def _ensure_dirs():
    """Create training data directories if they don't exist."""
    for d in (TRAINING_DIR, IMAGES_DIR, LABELS_DIR, RESULTS_DIR, PROFILES_DIR):
        d.mkdir(parents=True, exist_ok=True)


_ensure_dirs()


class TrainingDataManager:
    """Manages labeled receipt images for training and benchmarking."""

    def __init__(self):
        _ensure_dirs()

    # ─── Ingest ──────────────────────────────────────────────────────────

    def add_sample(
        self,
        image_path: str,
        ground_truth: dict,
        receipt_id: str | None = None,
        copy_image: bool = True,
    ) -> dict:
        """
        Add a labeled receipt image to the training set.

        Args:
            image_path: Path to the receipt image file.
            ground_truth: Dict with 'items' list (each has 'code' + 'quantity').
            receipt_id: Optional ID (auto-generated from filename if omitted).
            copy_image: If True, copies image into training_data/images/.

        Returns:
            Sample metadata dict.

        Raises:
            ValueError: If image doesn't exist or ground truth is invalid.
        """
        src = Path(image_path)
        if not src.exists():
            raise ValueError(f"Image not found: {image_path}")

        ext = src.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unsupported image format '{ext}'. "
                f"Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
            )

        # Validate ground truth
        self._validate_ground_truth(ground_truth)

        # Generate receipt ID
        if not receipt_id:
            receipt_id = src.stem

        # Ensure unique ID
        receipt_id = self._unique_id(receipt_id)

        # Copy or link image
        dest_image = IMAGES_DIR / f"{receipt_id}{ext}"
        if copy_image:
            shutil.copy2(str(src), str(dest_image))
        else:
            # Symlink (if on same filesystem)
            try:
                dest_image.symlink_to(src.resolve())
            except OSError:
                shutil.copy2(str(src), str(dest_image))

        # Save label
        label = {
            "receipt_id": receipt_id,
            "image_file": dest_image.name,
            "items": ground_truth["items"],
            "total_quantity": ground_truth.get(
                "total_quantity",
                sum(item["quantity"] for item in ground_truth["items"]),
            ),
            "receipt_type": ground_truth.get("receipt_type", "unknown"),
            "notes": ground_truth.get("notes", ""),
            "added_at": datetime.now().isoformat(),
        }

        label_path = LABELS_DIR / f"{receipt_id}.json"
        label_path.write_text(json.dumps(label, indent=2), encoding="utf-8")

        logger.info(
            f"Training sample added: {receipt_id} "
            f"({len(label['items'])} items, total_qty={label['total_quantity']})"
        )
        return label

    def add_sample_from_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        ground_truth: dict,
        receipt_id: str | None = None,
    ) -> dict:
        """
        Add a training sample from raw image bytes (for API uploads).

        Args:
            image_bytes: Raw image file content.
            filename: Original filename (for extension detection).
            ground_truth: Ground truth dict.
            receipt_id: Optional custom ID.

        Returns:
            Sample metadata dict.
        """
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"Unsupported format '{ext}'.")

        if not receipt_id:
            receipt_id = Path(filename).stem

        receipt_id = self._unique_id(receipt_id)

        dest_image = IMAGES_DIR / f"{receipt_id}{ext}"
        dest_image.write_bytes(image_bytes)

        self._validate_ground_truth(ground_truth)

        label = {
            "receipt_id": receipt_id,
            "image_file": dest_image.name,
            "items": ground_truth["items"],
            "total_quantity": ground_truth.get(
                "total_quantity",
                sum(item["quantity"] for item in ground_truth["items"]),
            ),
            "receipt_type": ground_truth.get("receipt_type", "unknown"),
            "notes": ground_truth.get("notes", ""),
            "added_at": datetime.now().isoformat(),
        }

        label_path = LABELS_DIR / f"{receipt_id}.json"
        label_path.write_text(json.dumps(label, indent=2), encoding="utf-8")

        logger.info(f"Training sample added from bytes: {receipt_id}")
        return label

    # ─── Query ───────────────────────────────────────────────────────────

    def list_samples(self) -> list[dict]:
        """List all training samples with their labels."""
        samples = []
        for label_file in sorted(LABELS_DIR.glob("*.json")):
            try:
                label = json.loads(label_file.read_text(encoding="utf-8"))
                image_path = IMAGES_DIR / label["image_file"]
                label["image_exists"] = image_path.exists()
                label["image_path"] = str(image_path)
                samples.append(label)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Skipping invalid label {label_file}: {e}")
        return samples

    def get_sample(self, receipt_id: str) -> dict | None:
        """Get a single training sample by ID."""
        label_file = LABELS_DIR / f"{receipt_id}.json"
        if not label_file.exists():
            return None
        label = json.loads(label_file.read_text(encoding="utf-8"))
        image_path = IMAGES_DIR / label["image_file"]
        label["image_exists"] = image_path.exists()
        label["image_path"] = str(image_path)
        return label

    def get_sample_pairs(self) -> list[tuple[str, dict]]:
        """
        Get all valid (image_path, ground_truth) pairs for benchmarking.

        Returns:
            List of (image_path, label_dict) tuples where image exists.
        """
        pairs = []
        for sample in self.list_samples():
            if sample.get("image_exists"):
                pairs.append((sample["image_path"], sample))
        return pairs

    def count_samples(self) -> int:
        """Return the number of training samples."""
        return len(list(LABELS_DIR.glob("*.json")))

    def delete_sample(self, receipt_id: str) -> bool:
        """Delete a training sample (image + label)."""
        label_file = LABELS_DIR / f"{receipt_id}.json"
        if not label_file.exists():
            return False

        try:
            label = json.loads(label_file.read_text(encoding="utf-8"))
            image_path = IMAGES_DIR / label["image_file"]
            if image_path.exists():
                image_path.unlink()
        except Exception:
            pass

        label_file.unlink()
        logger.info(f"Training sample deleted: {receipt_id}")
        return True

    def clear_all(self) -> int:
        """Delete all training samples. Returns count deleted."""
        count = 0
        for f in IMAGES_DIR.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
        for f in LABELS_DIR.iterdir():
            if f.is_file():
                f.unlink()
        logger.info(f"All training data cleared ({count} images removed)")
        return count

    # ─── Validation ──────────────────────────────────────────────────────

    @staticmethod
    def _validate_ground_truth(gt: dict):
        """Validate ground truth structure."""
        if "items" not in gt:
            raise ValueError("Ground truth must contain 'items' list.")
        if not isinstance(gt["items"], list):
            raise ValueError("'items' must be a list.")
        if len(gt["items"]) == 0:
            raise ValueError("'items' list cannot be empty.")

        for i, item in enumerate(gt["items"]):
            if "code" not in item:
                raise ValueError(f"Item {i} missing 'code' field.")
            if "quantity" not in item:
                raise ValueError(f"Item {i} missing 'quantity' field.")
            if not isinstance(item["code"], str) or not item["code"].strip():
                raise ValueError(f"Item {i} 'code' must be a non-empty string.")
            qty = item["quantity"]
            if not isinstance(qty, (int, float)) or qty <= 0:
                raise ValueError(f"Item {i} 'quantity' must be a positive number.")

    def _unique_id(self, base_id: str) -> str:
        """Ensure receipt_id is unique by appending a suffix if needed."""
        clean = base_id.strip().replace(" ", "_")
        if not (LABELS_DIR / f"{clean}.json").exists():
            return clean
        counter = 2
        while (LABELS_DIR / f"{clean}_{counter}.json").exists():
            counter += 1
        return f"{clean}_{counter}"

    # ─── Results Storage ─────────────────────────────────────────────────

    def save_benchmark_result(self, result: dict) -> str:
        """Save a benchmark run result to disk."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"benchmark_{ts}.json"
        path = RESULTS_DIR / filename
        path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        logger.info(f"Benchmark result saved: {filename}")
        return str(path)

    def list_benchmark_results(self) -> list[dict]:
        """List all saved benchmark results."""
        results = []
        for f in sorted(RESULTS_DIR.glob("benchmark_*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["_filename"] = f.name
                results.append(data)
            except Exception:
                pass
        return results

    def save_profile(self, profile: dict, name: str = "optimized") -> str:
        """Save an optimized parameter profile."""
        path = PROFILES_DIR / f"{name}.json"
        path.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
        logger.info(f"Profile saved: {name}")
        return str(path)

    def load_profile(self, name: str = "optimized") -> dict | None:
        """Load a saved parameter profile."""
        path = PROFILES_DIR / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_profiles(self) -> list[str]:
        """List available profile names."""
        return [f.stem for f in PROFILES_DIR.glob("*.json")]


# Module-level singleton
training_data_manager = TrainingDataManager()
