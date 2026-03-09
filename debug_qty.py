"""
Dump raw OCR detections for failing images to diagnose quantity parsing issues.
"""
import sys, os, time, logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.WARNING)

from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.engine import get_ocr_engine
from app.ocr.parser import ReceiptParser
from app.services.product_service import product_service

preprocessor = ImagePreprocessor()
ocr_engine = get_ocr_engine()
catalog = product_service.get_product_code_map()
parser = ReceiptParser(catalog)

# Test images that have qty issues
FAILING_IMAGES = [
    ("tests/sample_inputs/Media (2).jpg", "Handwritten - JKL=10(want 2), MNO=5(want 10)"),
    ("tests/sample_inputs/Media (4).jpg", "Handwritten - XYZ=30(want 10)"),
    ("tests/sample_inputs/Media (5).jpg", "Handwritten - VWX=24(want 2)"),
    ("uploads/upload_20260222_213048.png", "Boxed - ABC=2(want 5), DEF=3(want 12), JKL=2(want 4)"),
]

for img_rel, issue in FAILING_IMAGES:
    img_path = os.path.join(BASE_DIR, img_rel)
    if not os.path.exists(img_path):
        print(f"SKIP {img_rel} - not found")
        continue

    print(f"\n{'='*80}")
    print(f"  {os.path.basename(img_path)}")
    print(f"  Issue: {issue}")
    print(f"{'='*80}")

    # Preprocess
    processed, meta = preprocessor.preprocess(img_path)
    is_structured = preprocessor.detect_grid_structure(processed)
    cropped = preprocessor.crop_to_content(processed)
    print(f"  Structured: {is_structured}")
    print(f"  Image size: {cropped.shape[1]}x{cropped.shape[0]}")

    # OCR
    if is_structured:
        results = ocr_engine.extract_text_turbo(cropped)
    else:
        results = ocr_engine.extract_text_fast(cropped)

    print(f"\n  Raw OCR detections ({len(results)}):")
    for i, r in enumerate(results):
        bbox = r.get("bbox", [])
        y_center = (bbox[0][1] + bbox[2][1]) / 2 if len(bbox) >= 4 else 0
        x_center = (bbox[0][0] + bbox[2][0]) / 2 if len(bbox) >= 4 else 0
        width = bbox[2][0] - bbox[0][0] if len(bbox) >= 4 else 0
        print(f"    [{i:2d}] x={x_center:6.0f} y={y_center:6.0f} w={width:5.0f}  conf={r['confidence']:.3f}  text={r['text']!r}")

    # Parse with debug
    logging.getLogger("app.ocr.parser").setLevel(logging.DEBUG)

    # Group into lines first
    grouped = parser._group_into_lines(results)
    print(f"\n  Grouped lines ({len(grouped)}):")
    for i, line in enumerate(grouped):
        print(f"    Line {i}: y={line.get('y_center', 0):.0f}  text={line['text']!r}")

    # Full parse
    receipt_data = parser.parse(results)
    print(f"\n  Parsed items ({len(receipt_data.get('items', []))}):")
    for item in receipt_data.get("items", []):
        print(f"    {item['code']:>5} = {item['quantity']:>5.1f}  match={item['match_type']:<15}  raw={item.get('raw_text', '')!r}")

    logging.getLogger("app.ocr.parser").setLevel(logging.WARNING)

    # Also run color pass for handwritten to see differences
    if not is_structured:
        import cv2
        from app.config import IMAGE_MAX_DIMENSION
        original_color = cv2.imread(img_path)
        if original_color is not None:
            h, w = original_color.shape[:2]
            max_dim = IMAGE_MAX_DIMENSION
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                original_color = cv2.resize(original_color, None, fx=scale, fy=scale)
            color_results = ocr_engine.extract_text(original_color)
            print(f"\n  Color pass OCR detections ({len(color_results)}):")
            for i, r in enumerate(color_results):
                bbox = r.get("bbox", [])
                y_center = (bbox[0][1] + bbox[2][1]) / 2 if len(bbox) >= 4 else 0
                x_center = (bbox[0][0] + bbox[2][0]) / 2 if len(bbox) >= 4 else 0
                print(f"    [{i:2d}] x={x_center:6.0f} y={y_center:6.0f}  conf={r['confidence']:.3f}  text={r['text']!r}")

            color_grouped = parser._group_into_lines(color_results)
            print(f"\n  Color grouped lines ({len(color_grouped)}):")
            for i, line in enumerate(color_grouped):
                print(f"    Line {i}: y={line.get('y_center', 0):.0f}  text={line['text']!r}")

            color_data = parser.parse(color_results)
            print(f"\n  Color parsed items ({len(color_data.get('items', []))}):")
            for item in color_data.get("items", []):
                print(f"    {item['code']:>5} = {item['quantity']:>5.1f}  match={item['match_type']:<15}  raw={item.get('raw_text', '')!r}")

print("\n\nDone.")
