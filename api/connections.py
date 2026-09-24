"""Per-room connection tracking with pending-socket admission support."""

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from core.config import settings
from core.schemas import ServerEvent


class ConnectionManager:
    """Track active sockets for one room, or pending sockets at the gateway.

    All dictionary operations are synchronous on the ASGI event loop. Promotion is
    invoked inside the engine authentication lock, without performing network I/O.
    """

    def __init__(self) -> None:
        """Track pending sockets and the active connection for each client."""
        self.active_connections: dict[str, WebSocket] = {}
        self.pending: set[WebSocket] = set()
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}

    async def connect(self, client_id: str, websocket: WebSocket) -> bool:
        """Accept a pending socket, closing it if the pending cap is reached."""
        del client_id
        if len(self.pending) >= settings.server.max_pending_connections:
            await websocket.close(code=1013, reason="Too many pending connections")
            return False
        self.pending.add(websocket)
        self._send_locks[websocket] = asyncio.Lock()
        try:
            await websocket.accept()
        except BaseException:
            self.pending.discard(websocket)
            self._send_locks.pop(websocket, None)
            raise
        return True

    def promote(self, client_id: str, websocket: WebSocket) -> WebSocket | None:
        """Move a socket into the active map, returning any replaced socket."""
        self.pending.discard(websocket)
        self._send_locks.setdefault(websocket, asyncio.Lock())
        previous = self.active_connections.get(client_id)
        self.active_connections[client_id] = websocket
        return previous

    def release(self, websocket: WebSocket) -> None:
        """Drop a socket from pending tracking once another manager promotes it."""
        self.pending.discard(websocket)
        self._send_locks.pop(websocket, None)

    def owns(self, client_id: str, websocket: WebSocket) -> bool:
        """Return whether the socket is the current active connection for the client."""
        return self.active_connections.get(client_id) is websocket

    async def disconnect(self, client_id: str, websocket: WebSocket) -> bool:
        """Remove a socket if it is the active connection for the client."""
        self.pending.discard(websocket)
        self._send_locks.pop(websocket, None)
        if not self.owns(client_id, websocket):
            return False
        del self.active_connections[client_id]
        return True

    async def broadcast_global(self, event: ServerEvent) -> None:
        """Send an event to all active connections."""
        await self._broadcast(event)

    async def broadcast_except(self, client_id: str, event: ServerEvent) -> None:
        """Send an event to all active connections except one client."""
        await self._broadcast(event, exclude=client_id)

    async def _broadcast(self, event: ServerEvent, exclude: str | None = None) -> None:
        """Serialize and send an event to all active connections, optionally excluding one."""
        message = event.model_dump_json()
        sockets = [socket for item, socket in self.active_connections.items() if item != exclude]
        await asyncio.gather(*(self._send_text(socket, message) for socket in sockets))

    async def send_personal(self, client_id: str, event: ServerEvent) -> None:
        """Send an event to a single client's active connection."""
        websocket = self.active_connections.get(client_id)
        if websocket is not None:
            await self.send_socket(websocket, event)

    async def send_socket(self, websocket: WebSocket, event: ServerEvent) -> None:
        """Send an event to a specific socket."""
        await self._send_text(websocket, event.model_dump_json())

    async def _send_text(self, websocket: WebSocket, message: str) -> None:
        """Send serialized text under the socket's send lock, closing on failure."""
        lock = self._send_locks.get(websocket)
        if lock is None:
            return
        try:
            async with asyncio.timeout(5):
                async with lock:
                    await websocket.send_text(message)
        except (TimeoutError, RuntimeError, OSError, WebSocketDisconnect):
            await self.close_socket(websocket)

    @staticmethod
    async def close_socket(websocket: WebSocket, code: int = 1000) -> None:
        """Close a socket, tolerating errors and timeouts."""
        try:
            async with asyncio.timeout(2):
                await websocket.close(code=code)
        except Exception:
            pass

    async def close(self) -> None:
        """Close all active and pending sockets and clear the connection maps."""
        sockets = set(self.active_connections.values()) | self.pending
        await asyncio.gather(*(self.close_socket(socket) for socket in sockets))
        self.active_connections.clear()
        self.pending.clear()
        self._send_locks.clear()
