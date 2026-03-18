"""
WebSocket Manager for real-time batch processing updates.

Clients connect to ``ws://host:port/ws/batch/{batch_id}`` and receive
JSON messages as each file completes processing, eliminating the need
to poll ``GET /api/batch/{batch_id}``.

Message types:
    batch_started   — Batch processing has begun
    file_completed  — A single file finished (success or error)
    batch_completed — All files done (or batch failed/cancelled)
    error           — Server-side error on the WebSocket

Thread-safety: The manager uses an ``asyncio.Lock`` to guard the
subscriber dict and is safe to call from any coroutine.
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections grouped by batch_id.

    Usage:
        manager = get_ws_manager()
        await manager.connect(batch_id, websocket)
        ...
        await manager.broadcast(batch_id, {"type": "file_completed", ...})
    """

    def __init__(self):
        # batch_id → set of connected WebSocket clients
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, batch_id: str, websocket: WebSocket) -> None:
        """Accept and register a WebSocket client for a batch."""
        await websocket.accept()
        async with self._lock:
            if batch_id not in self._connections:
                self._connections[batch_id] = set()
            self._connections[batch_id].add(websocket)
        logger.debug(f"WebSocket connected: batch={batch_id}, clients={len(self._connections.get(batch_id, set()))}")

    async def disconnect(self, batch_id: str, websocket: WebSocket) -> None:
        """Remove a WebSocket client from a batch group."""
        async with self._lock:
            if batch_id in self._connections:
                self._connections[batch_id].discard(websocket)
                if not self._connections[batch_id]:
                    del self._connections[batch_id]
        logger.debug(f"WebSocket disconnected: batch={batch_id}")

    async def broadcast(self, batch_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to all clients subscribed to a batch."""
        async with self._lock:
            clients = list(self._connections.get(batch_id, set()))

        if not clients:
            return

        payload = json.dumps(message)
        dead: list[WebSocket] = []

        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        # Clean up disconnected clients
        if dead:
            async with self._lock:
                if batch_id in self._connections:
                    for ws in dead:
                        self._connections[batch_id].discard(ws)
                    if not self._connections[batch_id]:
                        del self._connections[batch_id]

    async def send_personal(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Send a message to a single client."""
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps(message))

    def has_subscribers(self, batch_id: str) -> bool:
        """Check if any clients are listening for a batch."""
        return batch_id in self._connections and len(self._connections[batch_id]) > 0

    async def close_batch(self, batch_id: str) -> None:
        """Close all connections for a completed batch."""
        async with self._lock:
            clients = list(self._connections.pop(batch_id, set()))

        for ws in clients:
            with contextlib.suppress(Exception):
                await ws.close()


# ─── Module-level singleton ──────────────────────────────────────────────────
_ws_manager: ConnectionManager | None = None


def get_ws_manager() -> ConnectionManager:
    """Get or create the WebSocket connection manager singleton."""
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = ConnectionManager()
    return _ws_manager
