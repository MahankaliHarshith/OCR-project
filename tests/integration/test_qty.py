"""Test quantity extraction with actual OCR detections from receipt scan."""
from app.ocr.parser import ReceiptParser

catalog = {
    'ABC': '1L Exterior Paint', 'XYZ': 'Paint Roller 9 inch',
    'PQR': '2 inch Masking Tape', 'MNO': '5L Interior Emulsion',
    'DEF': 'Paint Brush Set 3pc', 'GHI': 'Sandpaper Sheet',
    'JKL': 'Putty Knife 4 inch', 'STU': 'Wood Primer 500ml',
    'VWX': 'Wall Filler 1kg', 'RST': 'Turpentine 500ml'
}
parser = ReceiptParser(catalog)

# Simulate the ACTUAL grayscale OCR detections from logs
ocr_results = [
    {'text': 'tu',     'confidence': 0.9264, 'bbox': [[0,220],[100,220],[100,268],[0,268]]},
    {'text': 'Aouhly', 'confidence': 0.2646, 'bbox': [[120,178],[250,178],[250,226],[120,226]]},
    {'text': '[.',     'confidence': 0.5698, 'bbox': [[0,399],[50,399],[50,447],[0,447]]},
    {'text': 'ALC',    'confidence': 0.6096, 'bbox': [[60,390],[160,390],[160,438],[60,438]]},
    {'text': '-axk',   'confidence': 0.2773, 'bbox': [[170,342],[280,342],[280,390],[170,390]]},
    {'text': '2 .',    'confidence': 0.7828, 'bbox': [[0,608],[50,608],[50,656],[0,656]]},
    {'text': 'deF',    'confidence': 0.3454, 'bbox': [[60,568],[160,568],[160,616],[60,616]]},
    {'text': '5',      'confidence': 0.7639, 'bbox': [[170,618],[200,618],[200,666],[170,666]]},
    {'text': '3 4f_',  'confidence': 0.1868, 'bbox': [[210,593],[320,593],[320,641],[210,641]]},
    {'text': '%.',     'confidence': 0.8180, 'bbox': [[0,746],[50,746],[50,794],[0,794]]},
    {'text': '6n1',    'confidence': 0.2251, 'bbox': [[60,765],[160,765],[160,813],[60,813]]},
    {'text': 'Iq',     'confidence': 0.2134, 'bbox': [[170,689],[220,689],[220,737],[170,737]]},
    {'text': 'xp=',    'confidence': 0.0361, 'bbox': [[230,830],[320,830],[320,878],[230,878]]},
    {'text': '4 JpL',  'confidence': 0.3040, 'bbox': [[60,903],[200,903],[200,951],[60,951]]},
    {'text': '5',      'confidence': 0.9995, 'bbox': [[0,1079],[50,1079],[50,1127],[0,1127]]},
    {'text': 'MNo',    'confidence': 0.4904, 'bbox': [[60,1019],[160,1019],[160,1067],[60,1067]]},
    {'text': 'Io1f .', 'confidence': 0.4108, 'bbox': [[170,1010],[300,1010],[300,1058],[170,1058]]},
]

result = parser.parse(ocr_results)
print()
print(f"Found {len(result['items'])} items:")
for item in result['items']:
    print(f"  {item['code']} qty={item['quantity']} ({item['product']}) match={item['match_type']}")
print()
print("Expected: ABC=2, DEF=3, GHI=1, JKL=2, MNO=10")
