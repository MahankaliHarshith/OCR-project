"""Debug column-aware reassociation for messy receipt."""
import logging
import sys

# Set up logging to capture parser debug messages
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
handler.setFormatter(logging.Formatter('%(name)s | %(message)s'))

parser_logger = logging.getLogger('app.ocr.parser')
parser_logger.setLevel(logging.DEBUG)
parser_logger.addHandler(handler)

from app.services.receipt_service import receipt_service
from app.ocr.image_cache import get_image_cache
get_image_cache().clear()

print('='*60)
print('DEBUG: receipt_neat.jpg')
print('='*60)

result = receipt_service.process_receipt('test_images/receipt_neat.jpg')

print('\n--- RAW OCR ---')
for r in result['metadata'].get('raw_ocr', []):
    print(f'  text={r["text"]!r:25s}  conf={r["confidence"]:.3f}')

print('\n--- PARSED ITEMS ---')
for item in result['receipt_data']['items']:
    print(f'  {item["code"]:10s} qty={item["quantity"]}  match={item["match_type"]:15s}  raw={item.get("raw_text","")!r}')
    
print('\n--- UNPARSED ---')
for u in result['receipt_data'].get('unparsed_lines', []):
    print(f'  {u!r}')
