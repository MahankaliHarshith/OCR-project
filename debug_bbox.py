"""Debug bounding box positions for OCR detections."""
import sys
import logging
logging.disable(logging.CRITICAL)

from app.ocr.engine import OCREngine
from app.ocr.preprocessor import ImagePreprocessor
from app.ocr.image_cache import get_image_cache

get_image_cache().clear()
ocr_engine = OCREngine()
preprocessor = ImagePreprocessor()

for img in ['receipt_dense.jpg']:
    print(f'\n{"="*70}')
    print(f'  {img} - RAW OCR WITH POSITIONS')
    print(f'{"="*70}')
    
    processed_img, meta = preprocessor.preprocess(f'test_images/{img}')
    raw = ocr_engine.extract_text(processed_img)
    
    for r in raw:
        bbox = r.get('bbox', [])
        if bbox and len(bbox) >= 4:
            y_c = (float(bbox[0][1]) + float(bbox[2][1])) / 2
            x_c = (float(bbox[0][0]) + float(bbox[2][0])) / 2
        else:
            y_c = x_c = 0
        t = repr(r['text'])
        print(f'  {t:25s}  conf={r["confidence"]:.3f}  x={x_c:6.0f}  y={y_c:6.0f}')

print('\nDone.')
