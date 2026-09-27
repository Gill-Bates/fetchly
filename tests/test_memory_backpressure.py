#!/usr/bin/env python3
#
# tests/test_memory_backpressure.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""ENABLE_BACKPRESSURE / MEMORY_THRESHOLD_MB are documented as shedding new
jobs when memory headroom is gone, so the governor decision has to reach
/api/submit and /api/jobs/{id}/retry - not just exist on the governor."""

import asyncio
import unittest
import uuid
from unittest.mock import MagicMock, patch

from app.governor import Governor, GovernorConfig
from tests._support import WebAppTestCase


class CanAcceptJobTests(unittest.TestCase):
    def _governor(self, **config: object) -> Governor:
        governor = Governor()
        governor.configure(GovernorConfig(**config))  # type: ignore[arg-type]
        return governor

    def test_unconfigured_governor_accepts(self) -> None:
        self.assertTrue(asyncio.run(Governor().can_accept_job_async()))

    def test_rejects_below_threshold(self) -> None:
        governor = self._governor(memory_threshold_mb=256, enable_backpressure=True)
        with patch.object(governor, "get_memory_available_mb_async", return_value=64):
            self.assertFalse(asyncio.run(governor.can_accept_job_async()))

    def test_accepts_at_threshold(self) -> None:
        governor = self._governor(memory_threshold_mb=256, enable_backpressure=True)
        with patch.object(governor, "get_memory_available_mb_async", return_value=256):
            self.assertTrue(asyncio.run(governor.can_accept_job_async()))

    def test_accepts_when_memory_is_unknown(self) -> None:
        governor = self._governor(memory_threshold_mb=256, enable_backpressure=True)
        with patch.object(governor, "get_memory_available_mb_async", return_value=-1):
            self.assertTrue(asyncio.run(governor.can_accept_job_async()))

    def test_disabled_backpressure_accepts(self) -> None:
        governor = self._governor(memory_threshold_mb=256, enable_backpressure=False)
        with patch.object(governor, "get_memory_available_mb_async", return_value=0):
            self.assertTrue(asyncio.run(governor.can_accept_job_async()))


class SubmitUnderBackpressureTests(WebAppTestCase):
    """The queue is deliberately reported as not full, so a 503 can only come
    from the memory check."""

    def _empty_queue(self):
        return patch("app.routes.api.get_job_queue", return_value=MagicMock(full=lambda: False))

    def test_submit_is_rejected_with_503(self) -> None:
        with (
            self._empty_queue(),
            patch("app.routes.api.governor.can_accept_job_async", return_value=False),
        ):
            response = self.client.post(
                "/api/submit",
                data={
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "type": "audio",
                    "quality": "max",
                },
                headers={"X-CSRF-Token": self._csrf()},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("memory", response.json()["detail"].lower())

    def test_retry_is_rejected_with_503(self) -> None:
        from app import db

        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/v", "audio", "max", "error")
        with (
            self._empty_queue(),
            patch("app.routes.api.governor.can_accept_job_async", return_value=False),
        ):
            response = self.client.post(
                f"/api/jobs/{job_id}/retry",
                headers={"X-CSRF-Token": self._csrf()},
            )

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
