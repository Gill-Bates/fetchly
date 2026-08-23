#!/usr/bin/env python3
#
# app/routes/events.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server-sent event routes for real-time updates."""

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from itertools import count
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .auth import current_user, require_user
from ..common.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_shutdown_event: asyncio.Event | None = None
_SSE_KEEPALIVE_SECONDS = 5.0
_SSE_QUEUE_MAXSIZE = 32
_MAX_SSE_CONNECTIONS = 200
# NOTE: This broker is in-memory and process-local. Connected clients only
# receive events published by the same Python process. If deployment ever moves
# beyond a single process, replace this with a shared broker (for example Redis).
_sse_connections: set[asyncio.Queue[dict[str, Any]]] = set()
_job_sse_connections: defaultdict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
_sse_sequence = count(1)


class SSEStreamingResponse(StreamingResponse):
    """StreamingResponse variant that treats graceful-shutdown cancellation as normal."""

    async def listen_for_disconnect(self, receive):
        try:
            await super().listen_for_disconnect(receive)
        except asyncio.CancelledError:
            logger.debug("SSE disconnect listener cancelled during shutdown")


def publish_payload(payload: dict[str, Any]) -> None:
    """Broadcast a payload to all current SSE subscribers."""
    sequenced_payload = dict(payload)
    sequenced_payload.setdefault("seq", next(_sse_sequence))
    _publish_sse_payload(sequenced_payload)


def broadcast_shutdown() -> None:
    """Signal all SSE clients to close their connections gracefully."""
    shutdown_payload = {"type": "shutdown"}
    for subscriber in set(_sse_connections):
        _enqueue_sse_payload(subscriber, shutdown_payload)
    for subscribers in _job_sse_connections.values():
        for subscriber in set(subscribers):
            _enqueue_sse_payload(subscriber, shutdown_payload)


def _enqueue_sse_payload(subscriber: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Deliver an event and request a REST resync when the queue overflows."""
    try:
        subscriber.put_nowait(payload)
        return
    except asyncio.QueueFull:
        pass

    with suppress(asyncio.QueueEmpty):
        subscriber.get_nowait()

    if payload.get("type") == "shutdown":
        replacement = payload
    else:
        replacement = {
            "type": "resync_required",
            "seq": payload.get("seq"),
            "job_id": payload.get("id") or payload.get("job_id"),
        }
    try:
        subscriber.put_nowait(replacement)
    except asyncio.QueueFull:
        logger.debug("Dropping SSE resync marker because subscriber queue stayed full")


def _prune_empty_job_subscribers(job_id: str) -> None:
    subscribers = _job_sse_connections.get(job_id)
    if subscribers is not None and not subscribers:
        _job_sse_connections.pop(job_id, None)


def _publish_sse_payload(payload: dict[str, Any]) -> None:
    subscribers = set(_sse_connections)
    job_id = str(payload.get("id") or "").strip()
    if job_id:
        subscribers.update(_job_sse_connections.get(job_id, ()))

    for subscriber in subscribers:
        _enqueue_sse_payload(subscriber, payload)


def _subscribe_sse(queue_set: set[asyncio.Queue[dict[str, Any]]]) -> asyncio.Queue[dict[str, Any]]:
    subscriber: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    queue_set.add(subscriber)
    return subscriber


def _unsubscribe_sse(
    queue_set: set[asyncio.Queue[dict[str, Any]]],
    subscriber: asyncio.Queue[dict[str, Any]],
) -> None:
    queue_set.discard(subscriber)


def _format_sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _sse_stream(request: Request, subscriber: asyncio.Queue[dict[str, Any]]):
    yield "retry: 2000\n\n"
    try:
        while True:
            if current_user(request) is None:
                yield _format_sse({"type": "authentication_required"})
                return
            if _shutdown_event is not None and _shutdown_event.is_set():
                yield _format_sse({"type": "shutdown"})
                return
            if await request.is_disconnected():
                return

            # Race queue.get() against shutdown event
            get_task = asyncio.create_task(subscriber.get())
            wait_tasks: list[asyncio.Task[Any]] = [get_task]
            if _shutdown_event is not None:
                shutdown_task = asyncio.create_task(_shutdown_event.wait())
                wait_tasks.append(shutdown_task)
            else:
                shutdown_task = None

            try:
                done, _ = await asyncio.wait(
                    wait_tasks,
                    timeout=_SSE_KEEPALIVE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                get_task.cancel()
                if shutdown_task:
                    shutdown_task.cancel()
                raise

            # Cleanup pending tasks
            if shutdown_task and not shutdown_task.done():
                shutdown_task.cancel()
                try:
                    await shutdown_task
                except asyncio.CancelledError:
                    pass

            # Check what completed
            if not done:
                # Timeout - send keepalive
                get_task.cancel()
                try:
                    await get_task
                except asyncio.CancelledError:
                    pass
                if current_user(request) is None:
                    yield _format_sse({"type": "authentication_required"})
                    return
                yield ": keepalive\n\n"
                continue

            if shutdown_task in done:
                # Shutdown signaled
                get_task.cancel()
                try:
                    await get_task
                except asyncio.CancelledError:
                    pass
                yield _format_sse({"type": "shutdown"})
                return

            if get_task in done:
                # Got a message
                payload = get_task.result()
                if current_user(request) is None:
                    yield _format_sse({"type": "authentication_required"})
                    return
                yield _format_sse(payload)
    except asyncio.CancelledError:
        logger.debug("SSE stream cancelled during shutdown")
        return


def _build_sse_response(
    request: Request,
    subscriber: asyncio.Queue[dict[str, Any]],
    cleanup: Callable[[], None],
) -> StreamingResponse:
    async def _stream_with_cleanup():
        try:
            async for chunk in _sse_stream(request, subscriber):
                yield chunk
        except asyncio.CancelledError:
            logger.debug("SSE response cancelled during shutdown")
            return
        finally:
            cleanup()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return SSEStreamingResponse(_stream_with_cleanup(), media_type="text/event-stream", headers=headers)


@router.get("/events")
@limiter.limit("30/minute")
async def sse_events(request: Request, _user: str = Depends(require_user)):
    """Server-sent event stream for dashboard job updates."""
    if _active_connection_count() >= _MAX_SSE_CONNECTIONS:
        raise HTTPException(status_code=503, detail="Too many event streams")

    subscriber = _subscribe_sse(_sse_connections)
    return _build_sse_response(
        request,
        subscriber,
        lambda: _unsubscribe_sse(_sse_connections, subscriber),
    )


@router.get("/api/jobs/{job_id}/events")
@limiter.limit("30/minute")
async def sse_job_events(job_id: uuid.UUID, request: Request, _user: str = Depends(require_user)):
    """Server-sent event stream for a single job detail page."""
    if _active_connection_count() >= _MAX_SSE_CONNECTIONS:
        raise HTTPException(status_code=503, detail="Too many event streams")

    job_id_str = str(job_id)
    subscribers = _job_sse_connections[job_id_str]
    subscriber = _subscribe_sse(subscribers)

    def _cleanup() -> None:
        _unsubscribe_sse(subscribers, subscriber)
        _prune_empty_job_subscribers(job_id_str)

    return _build_sse_response(
        request,
        subscriber,
        _cleanup,
    )


def _active_connection_count() -> int:
    """Return the number of currently subscribed SSE streams."""
    return len(_sse_connections) + sum(
        len(subscribers) for subscribers in _job_sse_connections.values()
    )


def init_sse_shutdown_event() -> None:
    """Initialize the SSE shutdown event. Call once at startup."""
    global _shutdown_event
    _shutdown_event = asyncio.Event()


def signal_sse_shutdown() -> None:
    """Signal all SSE streams to exit gracefully."""
    if _shutdown_event is not None:
        _shutdown_event.set()
    broadcast_shutdown()
