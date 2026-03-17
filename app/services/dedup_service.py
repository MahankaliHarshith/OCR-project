"""
Duplicate Receipt Detection Service.

Three-layer approach:
1. Image Hash Dedup — perceptual hash (average hash) of the image.
   Similar-looking images produce similar hashes even if not byte-identical.
2. Content Fingerprint — deterministic hash of sorted item codes + quantities.
   Two receipts with the same items produce the same fingerprint.
3. User Confirmation — never silently rejects! Returns a warning with the
   similar receipt ID so the frontend can prompt "Save anyway?"
"""

import hashlib
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DedupService:
    """Detects duplicate receipt scans using image and content similarity."""

    # How far back to check for duplicates (hours)
    DEDUP_WINDOW_HOURS = 24

    # Perceptual hash hamming distance threshold
    # 0 = identical, ≤5 = very similar, >10 = different image
    PHASH_THRESHOLD = 5

    def compute_image_hash(self, image_path: str) -> str:
        """Compute a perceptual hash (average hash) of an image.

        Resizes to 8×8 grayscale, compares each pixel to mean.
        Returns a 16-char hex string (64-bit hash).
        Similar images produce hashes with low hamming distance.
        """
        try:
            from PIL import Image
            img = Image.open(image_path).convert("L").resize((8, 8), Image.LANCZOS)
            pixels = list(img.getdata())
            avg = sum(pixels) / len(pixels)
            bits = "".join("1" if p > avg else "0" for p in pixels)
            return hex(int(bits, 2))[2:].zfill(16)
        except Exception as e:
            logger.warning(f"Image hash computation failed: {e}")
            return ""

    def compute_content_fingerprint(self, items: List[Dict]) -> str:
        """Compute a content fingerprint from parsed receipt items.

        Creates a deterministic hash from sorted item codes and quantities.
        Two receipts with the same items (regardless of order) produce
        the same fingerprint.
        """
        if not items:
            return ""

        # Build canonical representation: sorted (code, qty) pairs
        pairs = []
        for item in items:
            code = (item.get("code") or "").upper().strip()
            qty = round(item.get("quantity", 0), 1)
            if code:
                pairs.append(f"{code}:{qty}")

        if not pairs:
            return ""  # all items had empty codes — no fingerprint

        pairs.sort()
        canonical = "|".join(pairs)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def hamming_distance(self, hash1: str, hash2: str) -> int:
        """Compute hamming distance between two hex hash strings.

        Returns 0 for identical, up to 64 for completely different.
        """
        if not hash1 or not hash2:
            return 64  # max distance = not similar
        # Pad to same length
        max_len = max(len(hash1), len(hash2))
        hash1 = hash1.zfill(max_len)
        hash2 = hash2.zfill(max_len)
        try:
            val1 = int(hash1, 16)
            val2 = int(hash2, 16)
            xor = val1 ^ val2
            return bin(xor).count("1")
        except ValueError:
            return 64

    def check_duplicate(
        self,
        image_hash: str,
        content_fingerprint: str,
        db_instance,
    ) -> Optional[Dict]:
        """Check if a receipt is a duplicate of an existing one.

        Checks both image hash similarity and content fingerprint match
        against receipts from the last DEDUP_WINDOW_HOURS.

        Returns:
            Dict with duplicate info if found, None if no duplicate.
        """
        try:
            recent = db_instance.get_recent_receipts_with_hashes(
                hours=self.DEDUP_WINDOW_HOURS
            )
        except Exception as e:
            logger.debug(f"Dedup check skipped (DB method unavailable): {e}")
            return None

        if not recent:
            return None

        best_match = None
        best_score = 0

        for receipt in recent:
            score = 0
            reasons = []

            # Check image hash similarity
            existing_hash = receipt.get("image_hash") or ""
            if image_hash and existing_hash:
                distance = self.hamming_distance(image_hash, existing_hash)
                if distance <= self.PHASH_THRESHOLD:
                    similarity = max(0, 100 - distance * 100 // 64)
                    score += 60
                    reasons.append(f"image_similarity={similarity}%")

            # Check content fingerprint (exact match)
            existing_fp = receipt.get("content_fingerprint") or ""
            if content_fingerprint and existing_fp and content_fingerprint == existing_fp:
                score += 40
                reasons.append("identical_items")

            if score > best_score:
                best_score = score
                best_match = {
                    "is_duplicate": score >= 60,
                    "confidence": score,
                    "similar_receipt_id": receipt.get("id"),
                    "similar_receipt_number": receipt.get("receipt_number"),
                    "scanned_at": receipt.get("created_at"),
                    "reasons": reasons,
                }

        if best_match and best_match["is_duplicate"]:
            logger.info(
                f"Duplicate detected: receipt #{best_match['similar_receipt_id']} "
                f"(score={best_match['confidence']}, reasons={best_match['reasons']})"
            )
            return best_match

        return None


# Singleton
dedup_service = DedupService()
