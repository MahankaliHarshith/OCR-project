"""Quick diagnostic: run full API pipeline on problem edge images."""
import sys, os, logging
sys.path.insert(0, '.')

# Enable parser debug logging to file
logging.basicConfig(level=logging.DEBUG, format='%(name)s: %(message)s',
                    filename='diag_edge_log.txt', filemode='w')
logging.getLogger('app.ocr.parser').setLevel(logging.DEBUG)
logging.getLogger('app.ocr.hybrid_engine').setLevel(logging.DEBUG)

from app.ocr.hybrid_engine import HybridOCREngine
from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.parser import ReceiptParser
from app.services.product_service import ProductService

pre = ImagePreprocessor()
hybrid = HybridOCREngine()
ps = ProductService()
prods = ps.get_all_products()
catalog = {}
for p in prods:
    code = p.get('code') or p.get('product_code') or ''
    name = p.get('name') or p.get('product_name') or ''
    if code:
        catalog[code] = name
parser = ReceiptParser(catalog)

for fname in ['edge_all_qty1.jpg', 'edge_total_items_confusion.jpg', 'receipt_dense.jpg']:
    path = os.path.join('test_images', fname)
    processed_image, preprocess_meta = pre.preprocess(path)
    is_structured = pre.detect_grid_structure(processed_image)
    _color_img = preprocess_meta.pop("_color_image", None)

    hybrid_result = hybrid.process_image(
        image_path=path,
        processed_image=processed_image,
        is_structured=is_structured,
        original_color=_color_img,
    )

    ocr_results = hybrid_result["ocr_detections"]
    print(f'\n{"="*60}')
    print(f'  {fname} — HYBRID OCR ({hybrid_result["engine_used"]}, passes={hybrid_result["ocr_passes"]})')
    print(f'{"="*60}')
    for det in ocr_results:
        bbox, text, conf = det['bbox'], det['text'], det['confidence']
        y = int((bbox[0][1] + bbox[2][1]) / 2)
        x = int((bbox[0][0] + bbox[2][0]) / 2)
        print(f'  x={x:4d} y={y:4d}  conf={conf:.2f}  "{text}"')

    print(f'\n  PARSING...')
    parsed = parser.parse(ocr_results, is_structured=is_structured)
    print(f'\n  ITEMS:')
    for it in parsed.get('items', []):
        print(f"    {it['code']}: qty={it['quantity']}, match={it.get('match_type','?')}")
    tv = parsed.get('total_verification', {})
    print(f"  total_qty_ocr = {tv.get('total_qty_ocr')}")
