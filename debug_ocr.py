"""Debug script to trace OCR pipeline on problem images."""
import logging
logging.basicConfig(level=logging.INFO, format='%(name)s | %(message)s')

from app.services.receipt_service import receipt_service
from app.ocr.engine import OCREngine
from app.ocr.preprocessor import image_preprocessor
ocr_engine = OCREngine()

# Clear cache first
from app.ocr.image_cache import get_image_cache
get_image_cache().clear()

for img in ['receipt_messy.jpg', 'receipt_dense.jpg']:
    print(f'\n{"="*65}')
    print(f'DEBUG: test_images/{img}')
    print(f'{"="*65}')
    
    # Get raw OCR with bounding boxes
    pre_result = image_preprocessor.preprocess(f'test_images/{img}')
    raw_ocr = ocr_engine.extract_text(pre_result['processed_path'], speed_mode='full')
    
    print('\n--- RAW OCR WITH POSITIONS ---')
    for r in raw_ocr:
        bbox = r.get('bbox', [])
        if bbox and len(bbox) >= 4:
            y_c = (float(bbox[0][1]) + float(bbox[2][1])) / 2
            x_c = (float(bbox[0][0]) + float(bbox[2][0])) / 2
        else:
            y_c = x_c = 0
        print(f'  text={r["text"]!r:25s}  conf={r["confidence"]:.3f}  x={x_c:6.0f}  y={y_c:6.0f}')
    
    # Now run full pipeline
    result = receipt_service.process_receipt(f'test_images/{img}')
    
    print('\n--- PARSED ITEMS ---')
    for item in result['receipt_data']['items']:
        print(f'  code={item["code"]:10s}  qty={str(item["quantity"]):<6}  raw={item.get("raw_text","")!r}')
