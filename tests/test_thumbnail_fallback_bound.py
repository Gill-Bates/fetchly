#!/usr/bin/env python3
#
# tests/test_thumbnail_fallback_bound.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The yt-dlp thumbnail fallback runs inside a small fixed concurrency budget."""

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routes import api


class _Probe:
    """Stand-in for the yt-dlp subprocess that records peak parallelism."""

    def __init__(self, duration: float) -> None:
        self.duration = duration
        self.running = 0
        self.peak = 0
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, _url: str) -> tuple[bytes, str]:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.duration)
        with self._lock:
            self.running -= 1
        return b"jpg", "image/jpeg"


class ThumbnailFallbackBoundTests(unittest.TestCase):
    def _run(self, probe: _Probe, *, wait: float, count: int) -> list[object]:
        async def _main() -> list[object]:
            api._thumb_extraction_capacity = asyncio.Semaphore(api._THUMB_EXTRACTION_MAX_CONCURRENT)
            return await asyncio.gather(
                *(api._extract_thumbnail_bounded("https://example.com/v") for _ in range(count)),
                return_exceptions=True,
            )

        with (
            patch.object(api, "_extract_thumbnail_with_ytdlp", probe),
            patch.object(api, "_THUMB_EXTRACTION_WAIT_SECONDS", wait),
        ):
            return asyncio.run(_main())

    def test_concurrent_fallbacks_never_exceed_the_bound(self) -> None:
        probe = _Probe(0.05)
        results = self._run(probe, wait=10.0, count=8)
        self.assertEqual(results, [(b"jpg", "image/jpeg")] * 8)
        self.assertLessEqual(probe.peak, api._THUMB_EXTRACTION_MAX_CONCURRENT)

    def test_saturated_budget_answers_503_instead_of_queueing(self) -> None:
        probe = _Probe(0.4)
        results = self._run(probe, wait=0.05, count=6)
        rejected = [r for r in results if isinstance(r, HTTPException)]
        self.assertEqual(len(rejected), 6 - api._THUMB_EXTRACTION_MAX_CONCURRENT)
        self.assertTrue(all(r.status_code == 503 and r.headers == {"Retry-After": "5"} for r in rejected))
        self.assertEqual(probe.calls, api._THUMB_EXTRACTION_MAX_CONCURRENT)
        self.assertLessEqual(probe.peak, api._THUMB_EXTRACTION_MAX_CONCURRENT)

    def test_cancelled_request_keeps_its_slot_until_the_subprocess_ends(self) -> None:
        probe = _Probe(0.3)

        async def _main() -> int:
            api._thumb_extraction_capacity = asyncio.Semaphore(1)
            first = asyncio.create_task(api._extract_thumbnail_bounded("https://example.com/v"))
            await asyncio.sleep(0.05)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            # The thread still runs, so the single slot is still taken.
            with self.assertRaises(HTTPException):
                await api._extract_thumbnail_bounded("https://example.com/v")
            await asyncio.sleep(0.4)
            await api._extract_thumbnail_bounded("https://example.com/v")
            return probe.peak

        with (
            patch.object(api, "_extract_thumbnail_with_ytdlp", probe),
            patch.object(api, "_THUMB_EXTRACTION_WAIT_SECONDS", 0.05),
        ):
            self.assertEqual(asyncio.run(_main()), 1)


if __name__ == "__main__":
    unittest.main()
