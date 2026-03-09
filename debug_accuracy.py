"""Debug: full pipeline for failing images - see parser decisions."""
import logging
logging.basicConfig(level=logging.DEBUG)

from app.services.receipt_service import receipt_service

for img in ['receipt_neat.jpg', 'receipt_faded.jpg']:
    print(f'\n{"="*60}')
    print(f'  FULL PIPELINE: {img}')
    print(f'{"="*60}')
    result = receipt_service.process_receipt(f'test_images/{img}')
    if result.get('success'):
        items = result['receipt_data']['items']
        for it in items:
            print(f"  code={it['code']:>8}  qty={it['quantity']}  conf={it.get('confidence',0):.3f}  raw={it.get('raw_text','?')!r}")
    else:
        print(f"  FAILED: {result.get('errors')}")
