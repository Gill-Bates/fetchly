#!/usr/bin/env python3
#
# tests/test_governor_semaphores.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""One budget per workload: app/worker.py takes transcode slots from a thread,
app/routes/{media,trim,lalal}.py take them from the event loop. Two separate
semaphores would each hand out the full count."""

import asyncio
import threading
import time
import unittest

# No tests._support import: app.governor needs no secret key, and conftest.py
# has already bootstrapped one before this module is imported.
from app.governor import Governor, GovernorConfig, SharedSemaphore


class SharedSemaphoreTests(unittest.TestCase):
    def test_a_coroutine_waits_for_a_slot_held_by_a_thread(self) -> None:
        semaphore = SharedSemaphore(1, poll_seconds=0.01)
        order: list[str] = []
        holding = threading.Event()

        def hold_from_a_thread() -> None:
            with semaphore:
                order.append("thread-in")
                holding.set()
                time.sleep(0.15)
                order.append("thread-out")

        async def take_it_after() -> None:
            thread = threading.Thread(target=hold_from_a_thread)
            thread.start()
            try:
                await asyncio.to_thread(holding.wait, 5.0)
                async with semaphore:
                    order.append("coro-in")
            finally:
                thread.join()

        asyncio.run(take_it_after())

        self.assertEqual(order, ["thread-in", "thread-out", "coro-in"])

    def test_a_cancelled_wait_leaves_no_slot_behind(self) -> None:
        semaphore = SharedSemaphore(1, poll_seconds=0.01)

        async def cancel_a_waiter() -> None:
            with semaphore:
                waiter = asyncio.create_task(semaphore.acquire_async())
                await asyncio.sleep(0.05)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter

        asyncio.run(cancel_a_waiter())

        self.assertTrue(semaphore.acquire())
        semaphore.release()


class GovernorSemaphoreIdentityTests(unittest.TestCase):
    def test_the_sync_and_async_accessors_share_one_object(self) -> None:
        governor = Governor()
        governor.configure(
            GovernorConfig(
                cpu_semaphore_limit=1,
                analysis_semaphore_limit=1,
                io_semaphore_limit=2,
                transcode_semaphore_limit=1,
            )
        )

        for name in ("cpu", "analysis", "io", "transcode"):
            with self.subTest(workload=name):
                self.assertIs(
                    getattr(governor, f"{name}_semaphore"),
                    getattr(governor, f"{name}_semaphore_sync"),
                )
