#!/usr/bin/env python3
#
# tests/test_metadata_executor_isolation.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""N2 regression: a metadata lookup that outlives its asyncio.wait_for()
timeout must not occupy the shared default executor.

asyncio.wait_for() only cancels the *awaiting* coroutine; a thread already
submitted via asyncio.to_thread()/run_in_executor(None, ...) keeps running to
completion on the shared default executor regardless, starving unrelated
asyncio.to_thread() work (DB calls, other routes) app-wide. The fix runs
metadata lookups on a dedicated ThreadPoolExecutor instead.
"""

import asyncio
import threading
import time
import unittest
import unittest.mock

from app.utils import youtube


class MetadataExecutorIsolationTests(unittest.TestCase):
    def test_load_video_info_async_uses_the_dedicated_executor(self) -> None:
        # Direct proof: patch asyncio's own run_in_executor and assert it was
        # handed youtube._METADATA_EXECUTOR, not None (the shared default
        # executor). The lookup call itself is stubbed out - only the
        # executor argument matters here.
        captured: list[object] = []
        loop = asyncio.new_event_loop()
        original = loop.run_in_executor

        def spy(executor, func, *args):
            captured.append(executor)
            return original(executor, func, *args)

        loop.run_in_executor = spy  # type: ignore[method-assign]
        stub_patcher = unittest.mock.patch.object(youtube, "load_video_info", return_value=None)
        stub_patcher.start()
        try:
            loop.run_until_complete(youtube.load_video_info_async("https://example.com/does-not-matter"))
        finally:
            stub_patcher.stop()
            loop.close()

        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0], youtube._METADATA_EXECUTOR)
        self.assertIsNot(captured[0], None)

    def test_saturating_metadata_lookups_does_not_starve_the_default_executor(self) -> None:
        """The actual regression: enough timed-out, still-running metadata

        calls to exceed the default executor's own worker count must not
        make unrelated asyncio.to_thread() work (DB calls, other routes)
        queue behind them.

        A single hung call is not enough to prove this either way: the
        default executor sizes itself at min(32, cpu_count + 4), so one
        occupied thread is invisible. Saturating it past that count is what
        actually reproduces the starvation the fix addresses.
        """
        release_slow_calls = threading.Event()

        def _slow_load_video_info(_url: str) -> None:
            release_slow_calls.wait(timeout=10)

        async def _scenario() -> float:
            loop = asyncio.get_running_loop()
            default_workers = getattr(loop._default_executor, "_max_workers", None)
            if default_workers is None:
                # Force the default executor into existence so its real size
                # is known, rather than assuming a value.
                await loop.run_in_executor(None, time.sleep, 0)
                default_workers = loop._default_executor._max_workers
            concurrent_lookups = default_workers + 5

            original = youtube.load_video_info
            youtube.load_video_info = _slow_load_video_info  # type: ignore[assignment]
            try:
                slow_tasks = [
                    asyncio.create_task(
                        asyncio.wait_for(youtube.load_video_info_async("https://example.com/slow"), timeout=0.2)
                    )
                    for _ in range(concurrent_lookups)
                ]
                for task in slow_tasks:
                    with self.assertRaises(asyncio.TimeoutError):
                        await task

                # The default executor (asyncio.to_thread) must still be
                # free: this is what N2 broke when metadata shared it.
                started = time.monotonic()
                await asyncio.to_thread(time.sleep, 0.01)
                return time.monotonic() - started
            finally:
                youtube.load_video_info = original  # type: ignore[assignment]
                release_slow_calls.set()

        elapsed = asyncio.run(_scenario())

        # A starved default executor would queue behind the 10s-capped slow
        # calls; a free one finishes near-instantly.
        self.assertLess(elapsed, 1.0)
