"""Test parser with OCR detections from the boxed template receipt."""
import io
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ["PYTHONIOENCODING"] = "utf-8"

from app.ocr.engine import OCREngine  # noqa: E402
from app.ocr.parser import ReceiptParser  # noqa: E402
from app.ocr.preprocessor import ImagePreprocessor  # noqa: E402

# Expected results
expected = {'ABC': 2, 'DEF': 3, 'GHI': 1, 'JKL': 2, 'MNO': 10}

# ── TEST 1: SIMULATED OCR DETECTIONS ──
print("=" * 60)
print("  TEST 1: SIMULATED OCR DETECTIONS (parser-only)")
print("=" * 60)

catalog = {
    'ABC': 'P1', 'DEF': 'P2', 'GHI': 'P3', 'JKL': 'P4', 'MNO': 'P5',
    'PQR': 'P6', 'STU': 'P7', 'VWX': 'P8', 'XYZ': 'P9', 'RST': 'P10'
}
p = ReceiptParser(catalog)

ocr = [
    {'text': 'RECEIPT', 'confidence': 0.9996, 'bbox': [[300,80],[622,80],[622,166],[300,166]]},
    {'text': 'Date:', 'confidence': 0.9997, 'bbox': [[60,180],[214,180],[214,248],[60,248]]},
    {'text': 'Receipt #:', 'confidence': 0.9993, 'bbox': [[480,183],[694,183],[694,251],[480,251]]},
    {'text': 'S.No', 'confidence': 0.9727, 'bbox': [[100,290],[198,290],[198,356],[100,356]]},
    {'text': 'PRODUCT CODE', 'confidence': 0.9424, 'bbox': [[220,276],[438,276],[438,342],[220,342]]},
    {'text': 'QUANTITY', 'confidence': 0.9797, 'bbox': [[460,276],[668,276],[668,342],[460,342]]},
    {'text': 'UNIT', 'confidence': 0.9688, 'bbox': [[680,290],[808,290],[808,356],[680,356]]},
    {'text': '(BLOCK CAPITALS)', 'confidence': 0.93, 'bbox': [[220,310],[436,310],[436,376],[220,376]]},
    {'text': '(NUMBER ONLY)', 'confidence': 0.9998, 'bbox': [[460,310],[666,310],[666,376],[460,376]]},
    {'text': '1', 'confidence': 1.0, 'bbox': [[120,370],[178,370],[178,438],[120,438]]},
    {'text': 'ABc', 'confidence': 0.9835, 'bbox': [[230,371],[358,371],[358,439],[230,439]]},
    {'text': '2', 'confidence': 1.0, 'bbox': [[520,373],[596,373],[596,441],[520,441]]},
    {'text': '2', 'confidence': 1.0, 'bbox': [[120,433],[178,433],[178,501],[120,501]]},
    {'text': 'DEF', 'confidence': 0.9969, 'bbox': [[227,437],[355,437],[355,505],[227,505]]},
    {'text': '3', 'confidence': 1.0, 'bbox': [[519,437],[597,437],[597,505],[519,505]]},
    {'text': '3', 'confidence': 1.0, 'bbox': [[120,496],[178,496],[178,564],[120,564]]},
    {'text': 'GHI', 'confidence': 0.9989, 'bbox': [[224,501],[352,501],[352,569],[224,569]]},
    {'text': '1', 'confidence': 0.9998, 'bbox': [[523,503],[599,503],[599,571],[523,571]]},
    {'text': '4', 'confidence': 1.0, 'bbox': [[121,559],[177,559],[177,627],[121,627]]},
    {'text': 'JKL', 'confidence': 0.994, 'bbox': [[229,564],[357,564],[357,632],[229,632]]},
    {'text': '2', 'confidence': 1.0, 'bbox': [[522,567],[598,567],[598,635],[522,635]]},
    {'text': '5', 'confidence': 1.0, 'bbox': [[121,623],[177,623],[177,691],[121,691]]},
    {'text': 'MNO', 'confidence': 0.9304, 'bbox': [[233,627],[361,627],[361,695],[233,695]]},
    {'text': 'I0', 'confidence': 0.4131, 'bbox': [[520,625],[596,625],[596,693],[520,693]]},
    {'text': '6', 'confidence': 1.0, 'bbox': [[119,687],[175,687],[175,755],[119,755]]},
    {'text': '7', 'confidence': 1.0, 'bbox': [[119,749],[175,749],[175,817],[119,817]]},
    {'text': '8', 'confidence': 1.0, 'bbox': [[119,813],[175,813],[175,881],[119,881]]},
    {'text': '9', 'confidence': 1.0, 'bbox': [[119,877],[175,877],[175,945],[119,945]]},
    {'text': '10', 'confidence': 1.0, 'bbox': [[122,940],[178,940],[178,1008],[122,1008]]},
    {'text': 'Total Items: [ 5 ]', 'confidence': 0.676, 'bbox': [[60,1060],[392,1060],[392,1130],[60,1130]]},
    {'text': 'Prepared By:', 'confidence': 0.9031, 'bbox': [[400,1070],[576,1070],[576,1140],[400,1140]]},
]

result = p.parse(ocr)
all_ok = True
found = {}
for item in result['items']:
    code = item['code']
    qty = int(item['quantity'])
    found[code] = qty
    exp_qty = expected.get(code)
    status = "OK" if exp_qty == qty else f"WRONG (expected {exp_qty})"
    if exp_qty != qty:
        all_ok = False
    print(f"  {code:>5s} = {qty:>3d}  [{item['match_type']}]  {status}")
for code, qty in expected.items():
    if code not in found:
        print(f"  {code:>5s} = MISSING (expected {qty})")
        all_ok = False
print(f"\nTest 1: {'PASS' if all_ok else 'FAIL'}")

# ── TEST 2: FULL PIPELINE ON ACTUAL IMAGE ──
print()
print("=" * 60)
print("  TEST 2: FULL PIPELINE (actual image through OCR)")
print("=" * 60)

img_path = "uploads/upload_20260222_213048.png"
if not os.path.exists(img_path):
    print(f"Image not found: {img_path}")
    sys.exit(1)

prep = ImagePreprocessor()
eng = OCREngine()

start = time.time()
gray_img, meta = prep.preprocess(img_path)
print(f"  Preprocessed: {gray_img.shape} in {meta.get('processing_time_ms', '?')}ms")

# Detect structured receipt and use turbo mode
is_structured = prep.detect_grid_structure(gray_img)
if is_structured:
    print(f"  ⚡ TURBO mode: grid detected, image {gray_img.shape[1]}x{gray_img.shape[0]}")
    ocr_results = eng.extract_text_turbo(gray_img)
else:
    print("  Standard mode (no grid detected)")
    ocr_results = eng.extract_text(gray_img)
ocr_time = time.time() - start
print(f"  OCR detections: {len(ocr_results)} in {ocr_time:.1f}s")

result2 = p.parse(ocr_results)
total_time = time.time() - start
print(f"  Parse result: {result2['total_items']} items, status={result2['processing_status']}")
print(f"  Total time: {total_time:.1f}s")
print()

all_ok2 = True
found2 = {}
for item in result2['items']:
    code = item['code']
    qty = int(item['quantity'])
    found2[code] = qty
    exp_qty = expected.get(code)
    status = "OK" if exp_qty == qty else f"WRONG (expected {exp_qty})"
    if exp_qty != qty:
        all_ok2 = False
    print(f"  {code:>5s} = {qty:>3d}  [{item['match_type']}]  {status}")
for code, qty in expected.items():
    if code not in found2:
        print(f"  {code:>5s} = MISSING (expected {qty})")
        all_ok2 = False
if result2['unparsed_lines']:
    print(f"\n  Unparsed: {[u['text'] for u in result2['unparsed_lines']]}")

print(f"\nTest 2: {'PASS' if all_ok2 else 'FAIL'}")

print()
print("=" * 60)
print(f"  OVERALL: {'ALL TESTS PASSED' if all_ok and all_ok2 else 'SOME TESTS FAILED'}")
print("=" * 60)
