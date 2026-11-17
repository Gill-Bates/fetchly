"""Regression tests for ``exclusive_file_lock`` cancellation safety."""

from __future__ import annotations

import asyncio
import fcntl
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (sets FETCHLY_SECRET_KEY before app imports)

from app.utils import fs


def _open_fd_count() -> int:
    return sum(1 for _ in Path("/proc/self/fd").iterdir())


class ExclusiveFileLockCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_file = Path(self._tmp.name) / "x.lock"

    async def _assert_reacquirable(self) -> None:
        async with fs.exclusive_file_lock(self.lock_file):
            pass

    async def test_cancel_during_acquisition_does_not_leak_lock(self) -> None:
        real_flock = fcntl.flock

        def slow_flock(fd: int, op: int) -> None:
            time.sleep(0.2)
            real_flock(fd, op)

        baseline = _open_fd_count()
        entered = asyncio.Event()

        async def worker() -> None:
            async with fs.exclusive_file_lock(self.lock_file):
                entered.set()
                await asyncio.sleep(10)

        with mock.patch.object(fs.fcntl, "flock", slow_flock):
            task = asyncio.create_task(worker())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.4)  # let any background acquisition finish

        await self._assert_reacquirable()
        self.assertEqual(_open_fd_count(), baseline)

    async def test_cancel_during_protected_operation_releases_lock(self) -> None:
        baseline = _open_fd_count()
        entered = asyncio.Event()

        async def worker() -> None:
            async with fs.exclusive_file_lock(self.lock_file):
                entered.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(worker())
        await asyncio.wait_for(entered.wait(), 2)
        with self.assertRaises(BlockingIOError):
            async with fs.exclusive_file_lock(self.lock_file):
                pass
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await self._assert_reacquirable()
        self.assertEqual(_open_fd_count(), baseline)

    async def test_contention_closes_descriptor(self) -> None:
        baseline = _open_fd_count()
        async with fs.exclusive_file_lock(self.lock_file):
            with self.assertRaises(BlockingIOError):
                async with fs.exclusive_file_lock(self.lock_file):
                    pass
        self.assertEqual(_open_fd_count(), baseline)


if __name__ == "__main__":
    unittest.main()
