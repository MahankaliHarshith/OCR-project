"""
Image Result Cache for OCR.

SHA-256 based cache that prevents re-processing identical images.
Common scenario: shop worker accidentally scans the same receipt twice,
or rescans after a failed upload — without cache, Azure is called again
and you're billed twice for the same image.

Cache is in-memory (LRU with max size) with disk persistence
so cached results survive server restarts.
"""

import hashlib
import json
import logging
import time
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    from app.metrics import record_cache_hit as _record_cache_hit, record_cache_miss as _record_cache_miss
except Exception:
    def _record_cache_hit():
        pass
    def _record_cache_miss():
        pass


class ImageCache:
    """
    LRU cache mapping image SHA-256 hashes to OCR results.

    Features:
        - Max size limit to prevent memory bloat
        - TTL (time-to-live) — cached results expire after N seconds
        - Thread-safe
        - Hit/miss statistics
        - Disk persistence (JSON) — survives server restarts
        - Debounced disk writes (at most once per 30s) to reduce I/O
    """

    DISK_WRITE_DEBOUNCE_SECONDS = 30  # Min interval between disk writes

    def __init__(self, max_size: int = 100, ttl_seconds: int = 3600, persist_path: Optional[str] = None):
        """
        Args:
            max_size: Maximum number of cached results (default: 100 images)
            ttl_seconds: Cache entry lifetime in seconds (default: 1 hour)
            persist_path: Path to JSON file for disk persistence (optional)
        """
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._persist_path = Path(persist_path) if persist_path else None

        # Statistics
        self.hits = 0
        self.misses = 0

        # Load from disk if available
        if self._persist_path:
            self._load_from_disk()

        logger.info(f"ImageCache initialized: max_size={max_size}, ttl={ttl_seconds}s, "
                     f"persist={'yes (' + str(self._persist_path) + ')' if self._persist_path else 'no'}, "
                     f"loaded={len(self._cache)} entries")

    def compute_hash(self, image_path: str) -> str:
        """Compute SHA-256 hash of an image file."""
        sha256 = hashlib.sha256()
        with open(image_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def get(self, image_hash: str) -> Optional[Dict]:
        """
        Look up cached OCR result by image hash.

        Returns:
            Cached result dict if found and not expired, else None.
        """
        with self._lock:
            if image_hash in self._cache:
                entry = self._cache[image_hash]
                age = time.time() - entry["cached_at"]

                if age <= self.ttl_seconds:
                    # Cache hit — move to end (most recently used)
                    self._cache.move_to_end(image_hash)
                    self.hits += 1
                    _record_cache_hit()

                    logger.info(
                        f"[Cache] HIT: hash={image_hash[:12]}..., "
                        f"age={age:.0f}s, engine={entry['result'].get('engine_used', '?')}"
                    )
                    return entry["result"]
                else:
                    # Expired — remove
                    del self._cache[image_hash]
                    logger.debug(f"[Cache] EXPIRED: hash={image_hash[:12]}..., age={age:.0f}s")

            self.misses += 1
            _record_cache_miss()
            return None

    def put(self, image_hash: str, result: Dict) -> None:
        """
        Store an OCR result in the cache.

        Args:
            image_hash: SHA-256 hash of the image
            result: The hybrid engine result dict to cache
        """
        with self._lock:
            # Evict oldest if at capacity
            while len(self._cache) >= self.max_size:
                evicted_key, _ = self._cache.popitem(last=False)
                logger.debug(f"[Cache] EVICTED oldest: {evicted_key[:12]}...")

            self._cache[image_hash] = {
                "result": result,
                "cached_at": time.time(),
            }

            logger.debug(
                f"[Cache] STORED: hash={image_hash[:12]}..., "
                f"cache_size={len(self._cache)}/{self.max_size}"
            )

            # Persist with debounce — at most once per 30s to reduce disk I/O
            now = time.time()
            if not hasattr(self, '_last_disk_write'):
                self._last_disk_write = 0.0
            if now - self._last_disk_write >= self.DISK_WRITE_DEBOUNCE_SECONDS:
                self._save_to_disk_unlocked()
                self._last_disk_write = now

    def get_stats(self) -> Dict:
        """Get cache performance statistics."""
        total = self.hits + self.misses
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total * 100, 1) if total > 0 else 0,
            "ttl_seconds": self.ttl_seconds,
            "azure_calls_saved": self.hits,
            "persisted": self._persist_path is not None,
        }

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0
            logger.info("[Cache] Cleared all cached entries")
        # Clear disk file too
        if self._persist_path and self._persist_path.exists():
            try:
                self._persist_path.unlink()
            except Exception:
                pass

    # ─── Disk Persistence ────────────────────────────────────────────────────

    def _save_to_disk(self):
        """Save cache entries to a JSON file (acquires lock)."""
        if not self._persist_path:
            return
        with self._lock:
            self._save_to_disk_unlocked()

    def _save_to_disk_unlocked(self):
        """Save cache entries to a JSON file (caller must hold self._lock)."""
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            # Snapshot data under lock
            now = time.time()
            data = {}
            for h, entry in self._cache.items():
                if now - entry["cached_at"] <= self.ttl_seconds:
                    data[h] = {
                        "result": self._make_json_safe(entry["result"]),
                        "cached_at": entry["cached_at"],
                    }
            with open(self._persist_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.debug(f"[Cache] Disk save failed: {e}")

    def _load_from_disk(self):
        """Load cache entries from disk on startup."""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, "r") as f:
                data = json.load(f)
            now = time.time()
            loaded = 0
            for h, entry in data.items():
                if now - entry["cached_at"] <= self.ttl_seconds:
                    self._cache[h] = entry
                    loaded += 1
                    if loaded >= self.max_size:
                        break
            logger.info(f"[Cache] Loaded {loaded} entries from disk")
        except Exception as e:
            logger.debug(f"[Cache] Disk load failed: {e}")

    @staticmethod
    def _make_json_safe(obj):
        """Convert numpy types to native Python for JSON serialization."""
        try:
            import numpy as np
            if isinstance(obj, dict):
                return {k: ImageCache._make_json_safe(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [ImageCache._make_json_safe(v) for v in obj]
            elif isinstance(obj, (np.integer,)):
                return int(obj)
            elif isinstance(obj, (np.floating,)):
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, np.bool_):
                return bool(obj)
        except ImportError:
            pass
        return obj


# ─── Singleton ───────────────────────────────────────────────────────────────

_cache: Optional[ImageCache] = None


def get_image_cache() -> ImageCache:
    """Get or create the image cache singleton."""
    global _cache
    if _cache is None:
        from app.config import IMAGE_CACHE_MAX_SIZE, IMAGE_CACHE_TTL, BASE_DIR
        persist_path = str(BASE_DIR / "data" / "image_cache.json")
        _cache = ImageCache(
            max_size=IMAGE_CACHE_MAX_SIZE,
            ttl_seconds=IMAGE_CACHE_TTL,
            persist_path=persist_path,
        )
    return _cache
