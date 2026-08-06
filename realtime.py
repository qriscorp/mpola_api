"""
In-process WebSocket connection registry + broadcast helper.

Mpola runs as a single process (or is meant to scale behind a sticky-session
load balancer) — this is a simple in-memory manager, not a pub/sub bus like
Redis. Good enough for live notification/wallet-balance pushes without
standing up new infrastructure; revisit if Mpola ever runs multiple
uncoordinated worker processes.
"""

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from logging_module import logger


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(user_id, []).append(websocket)

    def disconnect(self, user_id: str, websocket: WebSocket) -> None:
        sockets = self._connections.get(user_id)
        if not sockets:
            return
        if websocket in sockets:
            sockets.remove(websocket)
        if not sockets:
            self._connections.pop(user_id, None)

    async def send_to_user(self, user_id: str, message: dict[str, Any]) -> None:
        sockets = self._connections.get(user_id)
        if not sockets:
            return
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)

    def broadcast(self, user_id: str, message: dict[str, Any]) -> None:
        """Fire-and-forget send usable from synchronous code (schedules on
        the running event loop). Silently no-ops if there's no active loop
        (e.g. called from a background scheduler thread) — push notifications
        cover that case instead.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(self.send_to_user(user_id, message))
        except Exception as e:
            logger.error(f"WebSocket broadcast scheduling failed: {e}")


manager = ConnectionManager()
