import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ocr.parser import ReceiptParser

p = ReceiptParser({"ABC":"P1","XYZ":"P2","PQR":"P3","MNO":"P4"})

for code in ["DDD", "EEE", "FFF", "HHH", "LLL", "TTT"]:
    r = p.parse([{"text": f"{code} 5", "confidence": 0.90}])
    if r["items"]:
        prod = r["items"][0]["product"]
        mt = r["items"][0]["match_type"]
        print(f"{code} → {prod} ({mt})")
    else:
        print(f"{code} → NO ITEMS")
