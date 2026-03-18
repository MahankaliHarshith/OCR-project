"""
Template Learner.

Analyzes training receipt images to learn layout patterns:
    - Column positions (product code, quantity, price)
    - Spacing characteristics
    - Header/footer locations
    - Font size estimates

Templates speed up processing by telling the parser where to look
for specific fields, and help the preprocessor apply the right
enhancement strategy per receipt type.
"""

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class ReceiptTemplate:
    """Represents a learned receipt layout pattern."""

    def __init__(self, template_id: str):
        self.template_id = template_id
        self.samples_used = 0

        # Layout features (normalized 0.0-1.0 of image dimensions)
        self.code_column_x = 0.0  # Typical X position of product codes
        self.qty_column_x = 0.0  # Typical X position of quantities
        self.item_start_y = 0.0  # Y where items typically begin
        self.item_end_y = 1.0  # Y where items typically end
        self.avg_line_height = 0.0  # Average line height (normalized)
        self.avg_char_width = 0.0  # Average character width (normalized)

        # Detection statistics
        self.avg_detections = 0  # Average number of OCR detections
        self.avg_items = 0  # Average number of parsed items
        self.is_structured = False  # Boxed/grid layout?
        self.has_prices = False  # Contains price columns?
        self.has_line_numbers = False  # Contains serial numbers?

        # Optimal preprocessing hints
        self.preferred_blur = (3, 3)
        self.preferred_clahe_clip = 2.0
        self.preferred_max_dimension = 1800

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "samples_used": self.samples_used,
            "layout": {
                "code_column_x": round(self.code_column_x, 4),
                "qty_column_x": round(self.qty_column_x, 4),
                "item_start_y": round(self.item_start_y, 4),
                "item_end_y": round(self.item_end_y, 4),
                "avg_line_height": round(self.avg_line_height, 4),
                "avg_char_width": round(self.avg_char_width, 4),
            },
            "characteristics": {
                "avg_detections": self.avg_detections,
                "avg_items": self.avg_items,
                "is_structured": self.is_structured,
                "has_prices": self.has_prices,
                "has_line_numbers": self.has_line_numbers,
            },
            "preprocessing_hints": {
                "preferred_blur": list(self.preferred_blur),
                "preferred_clahe_clip": self.preferred_clahe_clip,
                "preferred_max_dimension": self.preferred_max_dimension,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReceiptTemplate":
        t = cls(data["template_id"])
        t.samples_used = data.get("samples_used", 0)
        layout = data.get("layout", {})
        t.code_column_x = layout.get("code_column_x", 0.0)
        t.qty_column_x = layout.get("qty_column_x", 0.0)
        t.item_start_y = layout.get("item_start_y", 0.0)
        t.item_end_y = layout.get("item_end_y", 1.0)
        t.avg_line_height = layout.get("avg_line_height", 0.0)
        t.avg_char_width = layout.get("avg_char_width", 0.0)
        chars = data.get("characteristics", {})
        t.avg_detections = chars.get("avg_detections", 0)
        t.avg_items = chars.get("avg_items", 0)
        t.is_structured = chars.get("is_structured", False)
        t.has_prices = chars.get("has_prices", False)
        t.has_line_numbers = chars.get("has_line_numbers", False)
        hints = data.get("preprocessing_hints", {})
        t.preferred_blur = tuple(hints.get("preferred_blur", [3, 3]))
        t.preferred_clahe_clip = hints.get("preferred_clahe_clip", 2.0)
        t.preferred_max_dimension = hints.get("preferred_max_dimension", 1800)
        return t


class TemplateLearner:
    """
    Learns receipt layout templates from training data.

    Analyzes OCR bounding boxes across multiple training images to
    identify consistent layout patterns (column positions, line spacing,
    item regions).
    """

    def __init__(self):
        self._preprocessor = None
        self._engine = None

    @property
    def preprocessor(self):
        if self._preprocessor is None:
            from app.ocr.preprocessor import ImagePreprocessor
            self._preprocessor = ImagePreprocessor()
        return self._preprocessor

    @property
    def engine(self):
        if self._engine is None:
            from app.ocr.hybrid_engine import get_hybrid_engine
            self._engine = get_hybrid_engine()
        return self._engine

    def learn_template(
        self,
        samples: list[tuple[str, dict]],
        template_id: str = "default",
    ) -> ReceiptTemplate:
        """
        Learn a receipt template from multiple training images.

        Analyzes OCR detection bounding boxes to find consistent
        layout patterns across all samples.

        Args:
            samples: List of (image_path, label_dict) pairs.
            template_id: Name for this template.

        Returns:
            Learned ReceiptTemplate.
        """
        if not samples:
            raise ValueError("No samples to learn from")

        template = ReceiptTemplate(template_id)
        template.samples_used = len(samples)

        all_code_x = []
        all_qty_x = []
        all_item_y_start = []
        all_item_y_end = []
        all_line_heights = []
        all_char_widths = []
        all_det_counts = []
        structured_votes = []
        has_prices_votes = []
        has_line_nums_votes = []

        for idx, (image_path, label) in enumerate(samples, 1):
            logger.info(f"Learning template [{idx}/{len(samples)}]: {label.get('receipt_id', 'unknown')}")

            try:
                features = self._extract_layout_features(image_path, label)
                if features:
                    if features["code_x"]:
                        all_code_x.extend(features["code_x"])
                    if features["qty_x"]:
                        all_qty_x.extend(features["qty_x"])
                    if features["item_y"]:
                        all_item_y_start.append(min(features["item_y"]))
                        all_item_y_end.append(max(features["item_y"]))
                    if features["line_heights"]:
                        all_line_heights.extend(features["line_heights"])
                    if features["char_widths"]:
                        all_char_widths.extend(features["char_widths"])
                    all_det_counts.append(features["detection_count"])
                    structured_votes.append(features["is_structured"])
                    has_prices_votes.append(features["has_prices"])
                    has_line_nums_votes.append(features["has_line_numbers"])
            except Exception as e:
                logger.warning(f"Failed to extract features from {image_path}: {e}")

        # Aggregate features
        if all_code_x:
            template.code_column_x = float(np.median(all_code_x))
        if all_qty_x:
            template.qty_column_x = float(np.median(all_qty_x))
        if all_item_y_start:
            template.item_start_y = float(np.median(all_item_y_start))
        if all_item_y_end:
            template.item_end_y = float(np.median(all_item_y_end))
        if all_line_heights:
            template.avg_line_height = float(np.median(all_line_heights))
        if all_char_widths:
            template.avg_char_width = float(np.median(all_char_widths))
        if all_det_counts:
            template.avg_detections = int(np.mean(all_det_counts))

        template.is_structured = sum(structured_votes) > len(structured_votes) / 2 if structured_votes else False
        template.has_prices = sum(has_prices_votes) > len(has_prices_votes) / 2 if has_prices_votes else False
        template.has_line_numbers = sum(has_line_nums_votes) > len(has_line_nums_votes) / 2 if has_line_nums_votes else False

        # Compute optimal preprocessing hints based on layout
        if template.is_structured:
            template.preferred_blur = (1, 1)  # Less blur for clear printed text
            template.preferred_clahe_clip = 1.5
        else:
            template.preferred_blur = (3, 3)  # More blur for handwriting noise
            template.preferred_clahe_clip = 2.5

        logger.info(
            f"Template '{template_id}' learned from {len(samples)} samples: "
            f"structured={template.is_structured}, "
            f"code_x={template.code_column_x:.3f}, "
            f"qty_x={template.qty_column_x:.3f}"
        )

        return template

    def _extract_layout_features(
        self,
        image_path: str,
        label: dict,
    ) -> dict | None:
        """
        Extract layout features from a single image using OCR detections.

        Returns normalized (0-1) positions of product codes and quantities.
        """
        import re

        import cv2

        img = cv2.imread(image_path)
        if img is None:
            return None

        h, w = img.shape[:2]
        if h == 0 or w == 0:
            return None

        # Run OCR to get bounding boxes
        processed, _meta = self.preprocessor.preprocess(image_path)
        hybrid_result = self.engine.process(processed, image_path)
        detections = hybrid_result.get("detections", [])

        expected_codes = {
            item["code"].upper() for item in label.get("items", [])
        }

        code_x_positions = []
        qty_x_positions = []
        item_y_positions = []
        line_heights = []
        char_widths = []
        has_prices = False
        has_line_numbers = False

        for det in detections:
            bbox = det.get("bbox", [])
            text = det.get("text", "").strip()

            if not bbox or len(bbox) < 4 or not text:
                continue

            # Compute normalized center positions
            xs = [pt[0] for pt in bbox]
            ys = [pt[1] for pt in bbox]
            cx = (min(xs) + max(xs)) / 2 / w  # normalized center X
            cy = (min(ys) + max(ys)) / 2 / h  # normalized center Y
            bh = (max(ys) - min(ys)) / h  # normalized height
            bw = (max(xs) - min(xs)) / w  # normalized width

            if bh > 0:
                line_heights.append(bh)
            if len(text) > 0 and bw > 0:
                char_widths.append(bw / len(text))

            # Check if this detection matches a known product code
            text_upper = text.upper().strip()
            if text_upper in expected_codes:
                code_x_positions.append(cx)
                item_y_positions.append(cy)

            # Check if text looks like a quantity (pure number)
            if re.match(r"^\d+\.?\d*$", text):
                qty_x_positions.append(cx)
                # Check for price-like numbers (> 100 usually means price)
                try:
                    val = float(text)
                    if val > 100:
                        has_prices = True
                except ValueError:
                    pass

            # Check for line numbers
            if re.match(r"^\d{1,2}\.$", text):
                has_line_numbers = True

        is_structured = self.preprocessor.detect_grid_structure(processed)

        return {
            "code_x": code_x_positions,
            "qty_x": qty_x_positions,
            "item_y": item_y_positions,
            "line_heights": line_heights,
            "char_widths": char_widths,
            "detection_count": len(detections),
            "is_structured": is_structured,
            "has_prices": has_prices,
            "has_line_numbers": has_line_numbers,
        }

    # ─── Template Persistence ────────────────────────────────────────────

    @staticmethod
    def save_template(template: ReceiptTemplate, directory: str = None) -> str:
        """Save a learned template to disk."""
        from app.training.data_manager import PROFILES_DIR
        save_dir = Path(directory) if directory else PROFILES_DIR
        save_dir.mkdir(parents=True, exist_ok=True)

        path = save_dir / f"template_{template.template_id}.json"
        path.write_text(
            json.dumps(template.to_dict(), indent=2),
            encoding="utf-8",
        )
        logger.info(f"Template saved: {path}")
        return str(path)

    @staticmethod
    def load_template(template_id: str, directory: str = None) -> ReceiptTemplate | None:
        """Load a template from disk."""
        from app.training.data_manager import PROFILES_DIR
        load_dir = Path(directory) if directory else PROFILES_DIR

        path = load_dir / f"template_{template_id}.json"
        if not path.exists():
            return None

        data = json.loads(path.read_text(encoding="utf-8"))
        return ReceiptTemplate.from_dict(data)

    @staticmethod
    def list_templates(directory: str = None) -> list[str]:
        """List available template IDs."""
        from app.training.data_manager import PROFILES_DIR
        load_dir = Path(directory) if directory else PROFILES_DIR

        templates = []
        for f in load_dir.glob("template_*.json"):
            tid = f.stem.replace("template_", "")
            templates.append(tid)
        return templates


# Module-level singleton
template_learner = TemplateLearner()
