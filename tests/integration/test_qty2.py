"""Test quantity extraction with BOTH color and grayscale OCR detections."""
from app.ocr.parser import ReceiptParser

catalog = {
    'ABC': '1L Exterior Paint', 'XYZ': 'Paint Roller 9 inch',
    'PQR': '2 inch Masking Tape', 'MNO': '5L Interior Emulsion',
    'DEF': 'Paint Brush Set 3pc', 'GHI': 'Sandpaper Sheet',
    'JKL': 'Putty Knife 4 inch', 'STU': 'Wood Primer 500ml',
    'VWX': 'Wall Filler 1kg', 'RST': 'Turpentine 500ml'
}
parser = ReceiptParser(catalog)

# Grayscale OCR detections
gray_results = [
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

# Color OCR detections
color_results = [
    {'text': '[tu',    'confidence': 0.6892, 'bbox': [[0,220],[100,220],[100,268],[0,268]]},
    {'text': 'Awtlz',  'confidence': 0.2649, 'bbox': [[120,171],[250,171],[250,219],[120,219]]},
    {'text': '[.',     'confidence': 0.4380, 'bbox': [[0,399],[50,399],[50,447],[0,447]]},
    {'text': 'ALC',    'confidence': 0.9143, 'bbox': [[60,395],[160,395],[160,443],[60,443]]},
    {'text': '-atk',   'confidence': 0.1775, 'bbox': [[170,345],[280,345],[280,393],[170,393]]},
    {'text': '2 .',    'confidence': 0.8774, 'bbox': [[0,607],[50,607],[50,655],[0,655]]},
    {'text': 'deF',    'confidence': 0.4038, 'bbox': [[60,566],[160,566],[160,614],[60,614]]},
    {'text': "-'",     'confidence': 0.1145, 'bbox': [[170,526],[220,526],[220,574],[170,574]]},
    {'text': '%.',     'confidence': 0.6245, 'bbox': [[0,746],[50,746],[50,794],[0,794]]},
    {'text': '6n1',    'confidence': 0.1490, 'bbox': [[60,765],[160,765],[160,813],[60,813]]},
    {'text': 'x8=',    'confidence': 0.1236, 'bbox': [[230,834],[320,834],[320,882],[230,882]]},
    {'text': 'V JkL',  'confidence': 0.1174, 'bbox': [[60,904],[200,904],[200,952],[60,952]]},
    {'text': '5 .',    'confidence': 0.8775, 'bbox': [[0,1076],[50,1076],[50,1124],[0,1124]]},
    {'text': 'MNo',    'confidence': 0.5824, 'bbox': [[60,1016],[160,1016],[160,1064],[60,1064]]},
    {'text': 'Io7} .', 'confidence': 0.2160, 'bbox': [[170,965],[300,965],[300,1013],[170,1013]]},
    {'text': 'Iqt',    'confidence': 0.0919, 'bbox': [[170,738],[250,738],[250,786],[170,786]]},
]

print("=== GRAYSCALE OCR PARSE ===")
gray_data = parser.parse(gray_results)
print(f"Found {len(gray_data['items'])} items:")
for item in gray_data['items']:
    print(f"  {item['code']} qty={item['quantity']} ({item['product']})")

print("\n=== COLOR OCR PARSE ===")
color_data = parser.parse(color_results)
print(f"Found {len(color_data['items'])} items:")
for item in color_data['items']:
    print(f"  {item['code']} qty={item['quantity']} ({item['product']})")

print("\n=== MERGED (gray primary + color supplement) ===")
primary_items = {item["code"]: item for item in gray_data.get("items", [])}
for alt_item in color_data.get("items", []):
    code = alt_item["code"]
    if code in primary_items and primary_items[code]["quantity"] == 1.0:
        alt_qty = alt_item["quantity"]
        if alt_qty != 1.0 and 1 <= alt_qty <= 99:
            print(f"  Supplement: {code} qty 1.0 → {alt_qty}")
            primary_items[code]["quantity"] = alt_qty

print("\nFinal items:")
for code, item in primary_items.items():
    print(f"  {code} qty={item['quantity']} ({item['product']})")

print("\nExpected: ABC=2, DEF=3, GHI=1, JKL=2, MNO=10")
