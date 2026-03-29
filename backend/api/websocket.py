import asyncio
import json
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Any

from config import get_config

logger = structlog.get_logger(__name__)

router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections for real-time dashboard updates."""

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.info("ws_client_connected", total=len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.info("ws_client_disconnected", total=len(self._connections))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast a message to all connected clients."""
        if not self._connections:
            return

        data = json.dumps(message, default=str, ensure_ascii=False)
        disconnected = []

        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_text(data)
                except Exception:
                    disconnected.append(ws)

            for ws in disconnected:
                self._connections.remove(ws)


# Singleton
ws_manager = ConnectionManager()


_WS_RECEIVE_TIMEOUT: int = get_config().ws_idle_timeout_sec  # override via APP_WS_IDLE_TIMEOUT_SEC


@router.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            try:
                # Keep connection alive, receive client messages if needed
                data = await asyncio.wait_for(
                    websocket.receive_text(), timeout=_WS_RECEIVE_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.info("ws_client_idle_timeout", timeout_sec=_WS_RECEIVE_TIMEOUT)
                await websocket.close(code=1000)
                await ws_manager.disconnect(websocket)
                return
            # Client can send ping/pong or commands
            if data == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)
