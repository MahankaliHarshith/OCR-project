"""Quick API test for total verification."""
import requests, json, sys

r = requests.post(
    "http://localhost:8000/api/receipts/scan",
    files={"file": open("test_images/receipt_neat.jpg", "rb")}
)
d = r.json()
tv = d.get("receipt_data", {}).get("total_verification", {})
with open("test_api_output.txt", "w") as f:
    f.write("=== API Response: total_verification ===\n")
    f.write(json.dumps(tv, indent=2, default=str))
    f.write(f"\n\nItems: {len(d.get('receipt_data', {}).get('items', []))}")
    f.write(f"\nSuccess: {d.get('success')}")
    f.write(f"\nTotal Match: {tv.get('total_qty_match')}")
print("Output written to test_api_output.txt")
