#!/usr/bin/env python3
#
# app/routes/websocket.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""WebSocket routes for real-time updates."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..session import SESSION_COOKIE, validate_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

# WebSocket configuration
_WS_PING_INTERVAL = 30.0  # Seconds between heartbeat pings
_WS_PONG_TIMEOUT = 10.0   # Max wait for pong response


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return value if value > 0 else default


_MAX_CONNECTIONS = _env_int("TUBEYOU_WS_MAX_CONNECTIONS", 100)
_MAX_CONNECTIONS_PER_IP = _env_int("TUBEYOU_WS_MAX_CONNECTIONS_PER_IP", 5)

# Active WebSocket connections
connections: set[WebSocket] = set()
_ip_counts: Counter[str] = Counter()
_pending_connections = 0


def _reserve_connection(client_ip: str) -> bool:
    global _pending_connections

    if len(connections) + _pending_connections >= _MAX_CONNECTIONS:
        return False
    if _ip_counts[client_ip] >= _MAX_CONNECTIONS_PER_IP:
        return False

    _pending_connections += 1
    _ip_counts[client_ip] += 1
    return True


def _release_connection(client_ip: str) -> None:
    global _pending_connections

    if _pending_connections > 0:
        _pending_connections -= 1

    current = _ip_counts.get(client_ip, 0)
    if current <= 1:
        _ip_counts.pop(client_ip, None)
    else:
        _ip_counts[client_ip] = current - 1


async def broadcast_payload(payload: dict[str, Any], sockets: list[WebSocket]) -> None:
    """Broadcast a payload to multiple WebSocket connections."""
    if not sockets:
        return
    await asyncio.gather(*(send_safe(ws, payload) for ws in sockets))


async def send_safe(ws: WebSocket, payload: dict[str, Any]) -> None:
    """Send a payload to a WebSocket, removing it on failure."""
    try:
        async with asyncio.timeout(5.0):
            await ws.send_json(payload)
    except (WebSocketDisconnect, ConnectionResetError, BrokenPipeError, TimeoutError):
        connections.discard(ws)
    except Exception:
        connections.discard(ws)
        raise


async def close_all_connections() -> None:
    """Close all active WebSocket connections."""
    for ws in list(connections):
        try:
            await ws.close()
        except Exception:
            pass
    connections.clear()


# ============================================================================
# Routes
# ============================================================================


@router.websocket("/ws")
async def ws_status(websocket: WebSocket):
    """WebSocket endpoint for real-time job status updates.

    Implements an application-level ping/pong heartbeat to detect stale connections.
    Clients should respond to 'ping' messages with 'pong'.
    """
    client_ip = websocket.client.host if websocket.client and websocket.client.host else "unknown"
    if not _reserve_connection(client_ip):
        await websocket.close(code=1008, reason="Server capacity exceeded")
        return

    ping_task: asyncio.Task[None] | None = None
    accepted = False

    try:
        if not validate_session(websocket.cookies.get(SESSION_COOKIE)):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        accepted = True
        connections.add(websocket)

        last_activity = asyncio.get_running_loop().time()

        async def _heartbeat() -> None:
            nonlocal last_activity
            while True:
                await asyncio.sleep(_WS_PING_INTERVAL)
                now = asyncio.get_running_loop().time()
                if now - last_activity > _WS_PING_INTERVAL + _WS_PONG_TIMEOUT:
                    logger.debug("WebSocket client idle timeout, closing connection")
                    try:
                        await websocket.close(code=1000, reason="idle timeout")
                    except Exception:
                        pass
                    return
                try:
                    await websocket.send_text("ping")
                except Exception:
                    return

        ping_task = asyncio.create_task(_heartbeat())

        while True:
            try:
                async with asyncio.timeout(_WS_PING_INTERVAL + _WS_PONG_TIMEOUT + 5.0):
                    message = await websocket.receive_text()
                # Update last activity on any message (pong, subscribe, etc.)
                last_activity = asyncio.get_running_loop().time()
                if message == "pong":
                    logger.debug("WebSocket pong received")
            except TimeoutError:
                logger.debug("WebSocket receive timeout, closing")
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        if accepted:
            connections.discard(websocket)
        _release_connection(client_ip)
