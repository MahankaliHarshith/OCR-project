"""
Async Batch Processing Service.

Provides background job processing for scanning multiple receipts
without blocking the API. Uses asyncio + ThreadPoolExecutor for
true parallel OCR processing.

Architecture:
    ┌──────────────────────────────────────────────────────────┐
    │  POST /api/receipts/batch-async                          │
    │   → Validates all files                                  │
    │   → Saves to disk                                        │
    │   → Creates batch job (returns batch_id immediately)     │
    │   → Spawns background task                               │
    └──────────────────────────────────────────────────────────┘
                            │
                ┌───────────▼───────────┐
                │  Background Worker    │
                │  ThreadPoolExecutor   │
                │  (up to N parallel)   │
                └───────────┬───────────┘
                            │
            ┌───────┬───────┼───────┬───────┐
            ▼       ▼       ▼       ▼       ▼
         File 1  File 2  File 3  File 4  File 5
         (OCR)   (OCR)   (OCR)   (OCR)   (OCR)
                            │
                ┌───────────▼───────────┐
                │  GET /api/batch/{id}  │
                │  → Poll for status    │
                │  → Stream results     │
                └───────────────────────┘

Batch States:
    pending    → Job created, not yet started
    processing → Worker is actively scanning files
    completed  → All files processed (some may have errors)
    failed     → Batch-level failure (e.g., worker crash)
"""

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────
MAX_BATCH_SIZE = 20            # Maximum files per batch
MAX_CONCURRENT_SCANS = 3       # Parallel OCR workers per batch
MAX_ACTIVE_BATCHES = 5         # Prevent resource exhaustion
BATCH_RESULT_TTL = 3600        # Seconds to keep completed batch results (1 hour)
MAX_STORED_BATCHES = 50        # Maximum stored batch results before cleanup


class BatchStatus(str, Enum):
    """Batch processing lifecycle states."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileStatus(str, Enum):
    """Individual file processing states."""
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class FileResult:
    """Result for a single file in the batch."""
    filename: str
    index: int
    status: FileStatus = FileStatus.PENDING
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    processing_time_ms: int = 0

    def to_dict(self) -> Dict:
        result = {
            "filename": self.filename,
            "index": self.index,
            "status": self.status.value,
            "processing_time_ms": self.processing_time_ms,
        }
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass
class BatchJob:
    """Tracks the state of a batch processing job."""
    batch_id: str
    created_at: float
    files: List[FileResult] = field(default_factory=list)
    status: BatchStatus = BatchStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    total_files: int = 0
    processed_count: int = 0
    success_count: int = 0
    error_count: int = 0
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_dict(self, include_results: bool = True) -> Dict:
        """Serialize batch job to dict for API response."""
        result = {
            "batch_id": self.batch_id,
            "status": self.status.value,
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(),
            "total_files": self.total_files,
            "processed": self.processed_count,
            "succeeded": self.success_count,
            "failed": self.error_count,
            "progress_percent": round(
                (self.processed_count / self.total_files * 100) if self.total_files > 0 else 0,
                1,
            ),
        }
        if self.started_at:
            result["started_at"] = datetime.fromtimestamp(self.started_at).isoformat()
        if self.completed_at:
            result["completed_at"] = datetime.fromtimestamp(self.completed_at).isoformat()
            result["total_time_ms"] = int((self.completed_at - self.started_at) * 1000)
        if include_results:
            result["files"] = [f.to_dict() for f in self.files]
        return result


class BatchProcessingService:
    """
    Manages async batch receipt processing.

    - Accepts file paths, returns batch_id immediately
    - Processes in background with configurable parallelism
    - Stores results in-memory with TTL-based cleanup
    - Thread-safe via asyncio locks
    """

    def __init__(self):
        self._batches: Dict[str, BatchJob] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_SCANS,
            thread_name_prefix="batch-ocr",
        )
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        logger.info(
            f"BatchProcessingService initialized: "
            f"max_concurrent={MAX_CONCURRENT_SCANS}, "
            f"max_batch_size={MAX_BATCH_SIZE}, "
            f"result_ttl={BATCH_RESULT_TTL}s"
        )

    async def start(self):
        """Start the cleanup background task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Batch service cleanup task started")

    async def shutdown(self):
        """Graceful shutdown: cancel active tasks, stop executor."""
        logger.info("Shutting down batch processing service...")
        # Cancel cleanup loop
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel active batch tasks
        async with self._lock:
            for batch in self._batches.values():
                if batch._task and not batch._task.done():
                    batch._task.cancel()
                    batch.status = BatchStatus.CANCELLED

        # Shutdown thread pool
        self._executor.shutdown(wait=False)
        logger.info("Batch processing service stopped")

    async def create_batch(
        self,
        file_paths: List[tuple],  # List of (filename, upload_path)
    ) -> BatchJob:
        """
        Create a new batch processing job.

        Args:
            file_paths: List of (original_filename, saved_path) tuples.

        Returns:
            BatchJob with batch_id and initial status.

        Raises:
            ValueError: If batch too large or too many active batches.
        """
        if len(file_paths) > MAX_BATCH_SIZE:
            raise ValueError(f"Batch too large. Maximum {MAX_BATCH_SIZE} files per batch.")

        if len(file_paths) == 0:
            raise ValueError("No files provided.")

        # Check active batch limit
        async with self._lock:
            active = sum(
                1 for b in self._batches.values()
                if b.status in (BatchStatus.PENDING, BatchStatus.PROCESSING)
            )
            if active >= MAX_ACTIVE_BATCHES:
                raise ValueError(
                    f"Too many active batches ({active}/{MAX_ACTIVE_BATCHES}). "
                    f"Wait for current batches to complete."
                )

        # Create batch job
        batch_id = uuid.uuid4().hex[:12]
        batch = BatchJob(
            batch_id=batch_id,
            created_at=time.time(),
            total_files=len(file_paths),
            files=[
                FileResult(filename=fname, index=i)
                for i, (fname, _) in enumerate(file_paths)
            ],
        )

        # Store and launch
        async with self._lock:
            self._batches[batch_id] = batch

        # Spawn background task
        loop = asyncio.get_event_loop()
        paths_only = [path for _, path in file_paths]
        batch._task = asyncio.create_task(
            self._process_batch(batch, paths_only)
        )

        logger.info(
            f"Batch {batch_id} created: {len(file_paths)} files, "
            f"active batches: {active + 1}"
        )
        return batch

    async def get_batch(self, batch_id: str) -> Optional[BatchJob]:
        """Get batch status and results by ID."""
        return self._batches.get(batch_id)

    async def list_batches(self, limit: int = 20) -> List[Dict]:
        """List recent batches (summary only, no file results)."""
        sorted_batches = sorted(
            self._batches.values(),
            key=lambda b: b.created_at,
            reverse=True,
        )[:limit]
        return [b.to_dict(include_results=False) for b in sorted_batches]

    async def cancel_batch(self, batch_id: str) -> bool:
        """Cancel a pending or processing batch."""
        batch = self._batches.get(batch_id)
        if not batch:
            return False
        if batch.status not in (BatchStatus.PENDING, BatchStatus.PROCESSING):
            return False

        if batch._task and not batch._task.done():
            batch._task.cancel()
        batch.status = BatchStatus.CANCELLED
        batch.completed_at = time.time()
        logger.info(f"Batch {batch_id} cancelled")
        return True

    # ─── Background Processing ────────────────────────────────────────────

    async def _process_batch(self, batch: BatchJob, file_paths: List[str]):
        """Background worker: process all files in a batch."""
        batch.status = BatchStatus.PROCESSING
        batch.started_at = time.time()

        logger.info(f"Batch {batch.batch_id}: processing started ({batch.total_files} files)")

        # Notify WebSocket subscribers that batch has started
        await self._ws_broadcast(batch.batch_id, {
            "type": "batch_started",
            "batch_id": batch.batch_id,
            "total_files": batch.total_files,
            "status": batch.status.value,
        })

        try:
            # Process files with bounded concurrency using semaphore
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)

            async def process_one(index: int, path: str):
                async with semaphore:
                    await self._process_single_file(batch, index, path)

            # Launch all file processing tasks
            tasks = [
                asyncio.create_task(process_one(i, path))
                for i, path in enumerate(file_paths)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            batch.status = BatchStatus.CANCELLED
            logger.info(f"Batch {batch.batch_id}: cancelled")
            raise
        except Exception as e:
            batch.status = BatchStatus.FAILED
            logger.error(f"Batch {batch.batch_id}: failed with {e}", exc_info=True)
        else:
            batch.status = BatchStatus.COMPLETED

        batch.completed_at = time.time()
        total_ms = int((batch.completed_at - batch.started_at) * 1000)

        logger.info(
            f"Batch {batch.batch_id}: {batch.status.value} — "
            f"{batch.success_count}/{batch.total_files} succeeded, "
            f"{batch.error_count} errors, {total_ms}ms total"
        )
        # Notify WebSocket subscribers that batch is done
        await self._ws_broadcast(batch.batch_id, {
            "type": "batch_completed",
            "batch_id": batch.batch_id,
            "status": batch.status.value,
            "total_files": batch.total_files,
            "succeeded": batch.success_count,
            "failed": batch.error_count,
            "total_time_ms": total_ms,
        })
    async def _process_single_file(self, batch: BatchJob, index: int, file_path: str):
        """Process a single file within a batch (runs OCR in thread pool)."""
        file_result = batch.files[index]
        file_result.status = FileStatus.PROCESSING

        start = time.time()
        try:
            # Import here to avoid circular imports
            from app.services.receipt_service import receipt_service

            # Run OCR in thread pool (CPU-bound work)
            result = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                receipt_service.process_receipt,
                file_path,
            )

            # Handle numpy serialization
            import numpy as np

            class NumpyEncoder(json.JSONEncoder):
                def default(self, obj):
                    if isinstance(obj, np.integer):
                        return int(obj)
                    if isinstance(obj, np.floating):
                        return float(obj)
                    if isinstance(obj, np.bool_):
                        return bool(obj)
                    if isinstance(obj, np.ndarray):
                        return obj.tolist()
                    return super().default(obj)

            safe_result = json.loads(json.dumps(result, cls=NumpyEncoder))

            file_result.status = FileStatus.SUCCESS
            file_result.data = safe_result
            batch.success_count += 1

        except Exception as e:
            file_result.status = FileStatus.ERROR
            file_result.error = str(e)
            batch.error_count += 1
            logger.warning(
                f"Batch {batch.batch_id} file [{index}] "
                f"'{file_result.filename}' failed: {e}"
            )

        file_result.processing_time_ms = int((time.time() - start) * 1000)
        batch.processed_count += 1

        # Notify WebSocket subscribers of per-file progress
        await self._ws_broadcast(batch.batch_id, {
            "type": "file_completed",
            "batch_id": batch.batch_id,
            "index": index,
            "filename": file_result.filename,
            "status": file_result.status.value,
            "processing_time_ms": file_result.processing_time_ms,
            "error": file_result.error,
            "processed": batch.processed_count,
            "total_files": batch.total_files,
            "progress_percent": round(
                (batch.processed_count / batch.total_files * 100) if batch.total_files > 0 else 0,
                1,
            ),
        })

    # ─── WebSocket Notifications ──────────────────────────────────────────

    async def _ws_broadcast(self, batch_id: str, message: Dict[str, Any]) -> None:
        """Send a progress message to all WebSocket subscribers for a batch."""
        try:
            from app.websocket import get_ws_manager
            manager = get_ws_manager()
            if manager.has_subscribers(batch_id):
                await manager.broadcast(batch_id, message)
        except Exception as e:
            # WebSocket errors must never break batch processing
            logger.debug(f"WebSocket broadcast error for batch {batch_id}: {e}")

    # ─── Cleanup ──────────────────────────────────────────────────────────

    async def _cleanup_loop(self):
        """Periodically remove expired batch results."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Batch cleanup error: {e}")

    async def _cleanup_expired(self):
        """Remove completed batches older than TTL."""
        now = time.time()
        expired = []

        async with self._lock:
            for batch_id, batch in self._batches.items():
                if batch.status in (BatchStatus.COMPLETED, BatchStatus.FAILED, BatchStatus.CANCELLED):
                    if batch.completed_at and (now - batch.completed_at) > BATCH_RESULT_TTL:
                        expired.append(batch_id)

            # Also enforce max stored batches (FIFO eviction)
            if len(self._batches) > MAX_STORED_BATCHES:
                sorted_ids = sorted(
                    self._batches.keys(),
                    key=lambda bid: self._batches[bid].created_at,
                )
                excess = len(self._batches) - MAX_STORED_BATCHES
                for bid in sorted_ids[:excess]:
                    if self._batches[bid].status not in (BatchStatus.PENDING, BatchStatus.PROCESSING):
                        expired.append(bid)

            for batch_id in set(expired):
                del self._batches[batch_id]

        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired batch results")


# ─── Module-level singleton ──────────────────────────────────────────────────
_batch_service: Optional[BatchProcessingService] = None


def get_batch_service() -> BatchProcessingService:
    """Get or create the batch processing service singleton."""
    global _batch_service
    if _batch_service is None:
        _batch_service = BatchProcessingService()
    return _batch_service
