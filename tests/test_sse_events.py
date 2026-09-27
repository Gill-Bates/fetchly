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
