"""Check Azure Document Intelligence integration status."""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOG_LEVEL", "WARNING")

print("=" * 60)
print("  AZURE DOCUMENT INTELLIGENCE — INTEGRATION STATUS")
print("=" * 60)

# 1. SDK Check
print("\n[1] SDK INSTALLATION")
sdk_ok = False
try:
    import azure.ai.documentintelligence as adi
    print(f"    azure-ai-documentintelligence: INSTALLED (v{adi.__version__})")
    sdk_ok = True
except ImportError:
    print("    azure-ai-documentintelligence: NOT INSTALLED")

try:
    import azure.core
    print("    azure-core: INSTALLED")
except ImportError:
    print("    azure-core: NOT INSTALLED")

# 2. Credentials Check
print("\n[2] CREDENTIALS")
endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()
key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()
ep_display = (endpoint[:50] + "...") if len(endpoint) > 50 else (endpoint or "NOT SET")
key_display = (key[:8] + "...") if key else "NOT SET"
print(f"    Endpoint: {ep_display}")
print(f"    API Key:  {key_display}")
creds_ok = bool(endpoint and key)
status_str = "Configured" if creds_ok else "Not configured - Azure OCR disabled"
print(f"    Status:   {'OK' if creds_ok else 'MISSING'} ({status_str})")

# 3. Config Check
print("\n[3] CONFIG (app/config.py)")
from app.config import (
    AZURE_DOC_INTEL_AVAILABLE, OCR_ENGINE_MODE, AZURE_MODEL_STRATEGY,
    AZURE_DAILY_PAGE_LIMIT, AZURE_MONTHLY_PAGE_LIMIT, AZURE_FREE_TIER_PAGES,
    LOCAL_CONFIDENCE_SKIP_THRESHOLD, IMAGE_QUALITY_GATE_ENABLED,
    AZURE_API_TIMEOUT, HYBRID_CROSS_VERIFY, AZURE_IMAGE_MAX_DIMENSION,
    AZURE_IMAGE_QUALITY
)
print(f"    AZURE_DOC_INTEL_AVAILABLE:       {AZURE_DOC_INTEL_AVAILABLE}")
print(f"    OCR_ENGINE_MODE:                 {OCR_ENGINE_MODE}")
print(f"    AZURE_MODEL_STRATEGY:            {AZURE_MODEL_STRATEGY}")
print(f"    AZURE_DAILY_PAGE_LIMIT:          {AZURE_DAILY_PAGE_LIMIT}")
print(f"    AZURE_MONTHLY_PAGE_LIMIT:        {AZURE_MONTHLY_PAGE_LIMIT}")
print(f"    AZURE_FREE_TIER_PAGES:           {AZURE_FREE_TIER_PAGES}")
print(f"    LOCAL_CONFIDENCE_SKIP_THRESHOLD: {LOCAL_CONFIDENCE_SKIP_THRESHOLD}")
print(f"    IMAGE_QUALITY_GATE_ENABLED:      {IMAGE_QUALITY_GATE_ENABLED}")
print(f"    AZURE_API_TIMEOUT:               {AZURE_API_TIMEOUT}s")
print(f"    HYBRID_CROSS_VERIFY:             {HYBRID_CROSS_VERIFY}")
print(f"    AZURE_IMAGE_MAX_DIMENSION:       {AZURE_IMAGE_MAX_DIMENSION}px")
print(f"    AZURE_IMAGE_QUALITY:             {AZURE_IMAGE_QUALITY}")

# 4. Usage Data
print("\n[4] AZURE USAGE DATA")
usage_file = Path(__file__).resolve().parent.parent / "data" / "azure_usage.json"
if usage_file.exists():
    with open(usage_file, "r", encoding="utf-8") as f:
        usage = json.load(f)
    print("    Usage file: EXISTS")
    print(f"    Content: {json.dumps(usage, indent=4)[:600]}")
else:
    print("    Usage file: NOT FOUND (no Azure calls made yet)")

# 5. Hybrid Engine Init
print("\n[5] HYBRID ENGINE INITIALIZATION")
try:
    from app.ocr.hybrid_engine import get_hybrid_engine
    he = get_hybrid_engine()
    print(f"    Mode:          {he.mode}")
    azure_loaded = "loaded" if he._azure_engine else "not loaded (lazy)"
    local_loaded = "loaded" if he._local_engine else "not loaded (lazy)"
    print(f"    Azure engine:  {azure_loaded}")
    print(f"    Local engine:  {local_loaded}")

    from app.ocr.azure_engine import is_azure_available
    azure_avail = is_azure_available()
    avail_str = "YES - ready to use" if azure_avail else "NO (credentials or SDK missing)"
    print(f"    Azure avail:   {avail_str}")
except Exception as e:
    print(f"    Error: {e}")

# 6. Image Cache Status
print("\n[6] IMAGE CACHE STATUS")
try:
    from app.ocr.image_cache import get_image_cache
    cache = get_image_cache()
    cache_file = Path(__file__).resolve().parent.parent / "data" / "image_cache.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
        print(f"    Cache file: EXISTS")
        print(f"    Cached entries: {len(cache_data.get('entries', {}))}")
    else:
        print("    Cache file: NOT FOUND")
except Exception as e:
    print(f"    Cache check: {e}")

# 7. How Azure helps
print("\n[7] HOW AZURE DOCUMENT INTELLIGENCE HELPS")
print("    Azure Receipt Model (prebuilt-receipt):")
print("      - Natively extracts items, quantities, prices from receipts")
print("      - Handles BOTH printed AND handwritten text")
print("      - ~1-2 seconds vs ~9 seconds local OCR")
print("      - Structured extraction = no regex parsing needed")
print("      - Cost: $0.01/page (free tier: 500 pages/month)")
print("")
print("    Azure Read Model (prebuilt-read):")
print("      - General handwriting recognition")
print("      - Best for messy/faded/unusual layouts")
print("      - Cost: $0.0015/page (cheapest)")
print("")
print("    Smart Routing (auto mode):")
print("      - Local EasyOCR runs FIRST (always free)")
print("      - Azure only called when local confidence < 0.85")
print("      - Image quality gate skips Azure for blurry/dark photos")
print("      - 24h image cache prevents duplicate Azure charges")
print("      - Daily/monthly limits prevent surprise bills")

# Verdict
print("\n" + "=" * 60)
if creds_ok and sdk_ok:
    print("  VERDICT: Azure Document Intelligence FULLY ACTIVE")
    print("  Hybrid mode: Local-first, Azure fallback when needed")
else:
    reasons = []
    if not sdk_ok:
        reasons.append("SDK not installed")
    if not creds_ok:
        reasons.append("env vars not set")
    print(f"  VERDICT: Azure OCR CODE COMPLETE but INACTIVE ({', '.join(reasons)})")
    print("")
    print("  To activate Azure OCR, set these environment variables:")
    print("    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://<resource>.cognitiveservices.azure.com/")
    print("    AZURE_DOCUMENT_INTELLIGENCE_KEY=<your-api-key>")
    print("")
    print("  Current behavior: 100% local EasyOCR (works fine, just slower)")
print("=" * 60)
