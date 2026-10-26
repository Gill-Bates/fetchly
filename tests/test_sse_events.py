#!/usr/bin/env python3
#
# tests/test_sse_events.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SSE subscriber lifecycle: registration must not outlive an abandoned stream.

Starlette's ``StreamingResponse.stream_response`` (ASGI spec >= 2.4) awaits
``send()`` for the response headers before ever touching the body iterator;
if the client already disconnected, ``send()`` raises ``OSError`` and the
generator's ``async for`` loop - and everything inside it - never runs. A
subscriber registered outside that generator would therefore never be
cleaned up.
"""

import asyncio
import gc
import uuid
from contextlib import suppress
from unittest.mock import patch

from fastapi import HTTPException

from app.routes import events
from tests._support import IsolatedDbTestCase


class SseSubscriberLeakTest(IsolatedDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        events._sse_connections.clear()
        events._job_sse_connections.clear()

    def test_abandoned_stream_leaves_no_subscriber(self) -> None:
        response = events._build_sse_response(
            request=object(),  # never touched: the generator body never runs
            subscribe=lambda: events._subscribe_sse(events._sse_connections),
            cleanup=lambda subscriber: events._unsubscribe_sse(events._sse_connections, subscriber),
        )

        # Closing a never-started async generator runs none of its body, not
        # even code before the first `yield` - this is what stands in for
        # Starlette abandoning the response before its first iteration.
        asyncio.run(response.body_iterator.aclose())

        self.assertEqual(len(events._sse_connections), 0)

    def test_abandoned_job_stream_leaves_no_subscriber(self) -> None:
        job_id = "test-job-id"
        subscribers = events._job_sse_connections[job_id]

        def _cleanup(subscriber: object) -> None:
            events._unsubscribe_sse(subscribers, subscriber)
            events._prune_empty_job_subscribers(job_id)

        response = events._build_sse_response(
            request=object(),
            subscribe=lambda: events._subscribe_sse(subscribers),
            cleanup=_cleanup,
        )

        asyncio.run(response.body_iterator.aclose())

        self.assertEqual(len(subscribers), 0)

    def test_driven_stream_still_registers_and_unregisters(self) -> None:
        """Guard against a fix that stops registration from happening at all."""
        response = events._build_sse_response(
            request=object(),
            subscribe=lambda: events._subscribe_sse(events._sse_connections),
            cleanup=lambda subscriber: events._unsubscribe_sse(events._sse_connections, subscriber),
        )

        async def _drive_one_chunk_then_abort() -> None:
            agen = response.body_iterator
            await agen.__anext__()
            self.assertEqual(len(events._sse_connections), 1)
            await agen.aclose()

        asyncio.run(_drive_one_chunk_then_abort())

        self.assertEqual(len(events._sse_connections), 0)


class SseConnectionCapTest(IsolatedDbTestCase):
    """``_MAX_SSE_CONNECTIONS`` is reserved before any stream starts."""

    def setUp(self) -> None:
        super().setUp()
        events._sse_connections.clear()
        events._job_sse_connections.clear()
        patcher = patch.object(events, "_MAX_SSE_CONNECTIONS", 5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, events, "_reserved_connections", events._reserved_connections)
        events._reserved_connections = 0

    @staticmethod
    def _response(slot: events._ConnectionSlot, subscribe=None):
        return events._build_sse_response(
            request=object(),
            subscribe=subscribe or (lambda: events._subscribe_sse(events._sse_connections)),
            cleanup=lambda subscriber: events._unsubscribe_sse(events._sse_connections, subscriber),
            slot=slot,
        )

    def test_concurrent_reservations_never_exceed_the_cap(self) -> None:
        async def _try() -> events._ConnectionSlot | None:
            await asyncio.sleep(0)
            try:
                return events._reserve_connection_slot()
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 503)
                return None

        async def _run() -> list[events._ConnectionSlot | None]:
            return await asyncio.gather(*(_try() for _ in range(20)))

        slots = asyncio.run(_run())
        granted = [slot for slot in slots if slot is not None]
        self.assertEqual(len(granted), 5)
        self.assertEqual(events._reserved_connections, 5)
        for slot in granted:
            slot.release()
            slot.release()  # idempotent
        self.assertEqual(events._reserved_connections, 0)

    def test_slot_released_after_normal_close(self) -> None:
        response = self._response(events._reserve_connection_slot())

        async def _drive() -> None:
            agen = response.body_iterator
            await agen.__anext__()
            self.assertEqual(events._reserved_connections, 1)
            await agen.aclose()

        asyncio.run(_drive())
        self.assertEqual(events._reserved_connections, 0)

    def test_slot_released_when_client_aborts_before_first_iteration(self) -> None:
        response = self._response(events._reserve_connection_slot())
        self.assertEqual(events._reserved_connections, 1)

        async def _send(_message: dict) -> None:
            raise OSError("client went away")

        async def _receive() -> dict:
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "headers": []}
        with suppress(OSError, Exception):
            asyncio.run(response(scope, _receive, _send))
        self.assertEqual(events._reserved_connections, 0)
        self.assertEqual(len(events._sse_connections), 0)

    def test_slot_released_when_response_is_dropped_unused(self) -> None:
        response = self._response(events._reserve_connection_slot())
        self.assertEqual(events._reserved_connections, 1)
        del response
        gc.collect()
        self.assertEqual(events._reserved_connections, 0)

    def test_slot_released_after_error_while_subscribing(self) -> None:
        def _broken() -> object:
            raise RuntimeError("subscribe failed")

        response = self._response(events._reserve_connection_slot(), subscribe=_broken)

        async def _drive() -> None:
            with self.assertRaises(RuntimeError):
                await response.body_iterator.__anext__()

        asyncio.run(_drive())
        self.assertEqual(events._reserved_connections, 0)

    def test_abandoned_job_requests_leave_no_dictionary_entries(self) -> None:
        handler = events.sse_job_events.__wrapped__
        for _ in range(25):
            response = asyncio.run(handler(uuid.uuid4(), object(), "user"))
            asyncio.run(response.body_iterator.aclose())
            del response
        gc.collect()
        self.assertEqual(len(events._job_sse_connections), 0)
        self.assertEqual(events._reserved_connections, 0)

    def test_job_stream_registers_lazily_and_prunes_on_close(self) -> None:
        job_id = uuid.uuid4()
        response = asyncio.run(events.sse_job_events.__wrapped__(job_id, object(), "user"))
        self.assertNotIn(str(job_id), events._job_sse_connections)

        async def _drive() -> None:
            agen = response.body_iterator
            await agen.__anext__()
            self.assertEqual(len(events._job_sse_connections[str(job_id)]), 1)
            await agen.aclose()

        asyncio.run(_drive())
        self.assertNotIn(str(job_id), events._job_sse_connections)
        self.assertEqual(events._reserved_connections, 0)
