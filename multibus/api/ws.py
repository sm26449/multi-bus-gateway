# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The WebSocket fan-out: a bounded set of live connections, broadcast
concurrently with a per-send timeout so one wedged client cannot stall the
others or the event loop."""
import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import WebSocket

# kept as the api channel (not __name__) so a /ws line still reads
# "multibus.api" for whoever filters or captures on that logger
logger = logging.getLogger("multibus.api")


class WebSocketManager:
    """Manages WebSocket connections and broadcasts."""

    MAX_CONNECTIONS = 64        # bound FD/memory use; a monitor UI needs a handful

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> bool:
        await websocket.accept()
        async with self.lock:
            n = len(self.active_connections)
            refused = n >= self.MAX_CONNECTIONS
            if not refused:
                self.active_connections.add(websocket)
        if refused:
            # refuse rather than accumulate to FD exhaustion. Close OUTSIDE the
            # lock — the close handshake against a wedged client must not hold
            # self.lock and stall connect/disconnect/broadcast (all share it).
            logger.warning("WebSocket refused: %d active (cap %d)", n, self.MAX_CONNECTIONS)
            await websocket.close(code=1013)   # try again later
            return False
        logger.info(f"WebSocket connected. Active: {n + 1}")
        return True

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            self.active_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Active: {len(self.active_connections)}")

    async def broadcast(self, message: Dict):
        """Broadcast message to all connected clients."""
        # Snapshot the connections under the lock, then send OUTSIDE it: a slow
        # or wedged client's send_text must not hold the lock and stall
        # connect/disconnect and every other broadcast. Each send is bounded by
        # a timeout so one stuck socket can't block the whole fan-out.
        async with self.lock:
            conns = list(self.active_connections)
        if not conns:
            return
        data = json.dumps(message)

        # Fan out CONCURRENTLY: a sequential loop makes one slow client add its
        # full 5 s timeout to every client behind it (up to MAX_CONNECTIONS×5 s
        # per broadcast, backing up the event loop). gather bounds the whole
        # fan-out to the slowest single send instead.
        async def _send(conn):
            try:
                await asyncio.wait_for(conn.send_text(data), timeout=5)
                return None
            except Exception:  # noqa: BLE001 — timeout or send error → drop it
                return conn
        disconnected = [c for c in await asyncio.gather(*(_send(c) for c in conns)) if c]
        if disconnected:
            async with self.lock:
                for conn in disconnected:
                    self.active_connections.discard(conn)
