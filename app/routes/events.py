#!/usr/bin/env python3
#
# app/routes/events.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server-sent event routes for real-time updates."""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .auth import require_user
from ..common.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_SSE_KEEPALIVE_SECONDS = 15.0
_SSE_QUEUE_MAXSIZE = 32
# NOTE: This broker is in-memory and process-local. Connected clients only
# receive events published by the same Python process. If deployment ever moves
# beyond a single process, replace this with a shared broker (for example Redis).
_sse_connections: set[asyncio.Queue[dict[str, Any]]] = set()
_job_sse_connections: defaultdict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)


class SSEStreamingResponse(StreamingResponse):
    """StreamingResponse variant that treats graceful-shutdown cancellation as normal."""

    async def listen_for_disconnect(self, receive):
        try:
            await super().listen_for_disconnect(receive)
        except asyncio.CancelledError:
            logger.debug("SSE disconnect listener cancelled during shutdown")


def publish_payload(payload: dict[str, Any]) -> None:
    """Broadcast a payload to all current SSE subscribers."""
    _publish_sse_payload(payload)


def _enqueue_sse_payload(subscriber: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Best-effort delivery: drop the oldest event when a subscriber queue is full."""
    try:
        subscriber.put_nowait(payload)
        return
    except asyncio.QueueFull:
        pass

    try:
        subscriber.get_nowait()
    except asyncio.QueueEmpty:
        return

    try:
        subscriber.put_nowait(payload)
    except asyncio.QueueFull:
        logger.debug("Dropping SSE event because subscriber queue stayed full")


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
            if await request.is_disconnected():
                return
            try:
                async with asyncio.timeout(_SSE_KEEPALIVE_SECONDS):
                    payload = await subscriber.get()
            except TimeoutError:
                yield ": keepalive\n\n"
                continue

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
    subscriber = _subscribe_sse(_sse_connections)
    return _build_sse_response(
        request,
        subscriber,
        lambda: _unsubscribe_sse(_sse_connections, subscriber),
    )


@router.get("/api/jobs/{job_id}/events")
@limiter.limit("30/minute")
async def sse_job_events(job_id: str, request: Request, _user: str = Depends(require_user)):
    """Server-sent event stream for a single job detail page."""
    subscribers = _job_sse_connections[job_id]
    subscriber = _subscribe_sse(subscribers)

    def _cleanup() -> None:
        _unsubscribe_sse(subscribers, subscriber)
        _prune_empty_job_subscribers(job_id)

    return _build_sse_response(
        request,
        subscriber,
        _cleanup,
    )
