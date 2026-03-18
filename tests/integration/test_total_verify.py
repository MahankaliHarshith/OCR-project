"""Quick test: Bill Total Verification on test images."""
from app.services.receipt_service import receipt_service

images = [
    "test_images/receipt_neat.jpg",
    "test_images/receipt_messy.jpg",
    "test_images/receipt_dense.jpg",
    "test_images/receipt_faded.jpg",
    "test_images/receipt_dark_ink.jpg",
]

for img in images:
    print(f"\n{'='*60}")
    print(f"  IMAGE: {img}")
    print(f"{'='*60}")
    result = receipt_service.process_receipt(img)
    rd = result.get("receipt_data", {})
    tv = rd.get("total_verification", {})

    print("  ITEMS:")
    for item in rd.get("items", []):
        print(f"    {item['code']:8s}  qty={item['quantity']}")

    computed = sum(it["quantity"] for it in rd.get("items", []))
    print("\n  TOTAL VERIFICATION:")
    print(f"    OCR Total:      {tv.get('ocr_total', tv.get('total_qty_ocr', 'N/A'))}")
    print(f"    Computed Total:  {tv.get('computed_total', tv.get('total_qty_computed', 'N/A'))}")
    print(f"    Match:           {tv.get('total_qty_match', 'N/A')}")
    print(f"    Status:          {tv.get('verification_status', tv.get('verification_method', 'N/A'))}")
    if tv.get("total_line_text"):
        print(f"    Total Line:      {tv.get('total_line_text')!r}")

    print(f"\n  Success: {result.get('success')}")
