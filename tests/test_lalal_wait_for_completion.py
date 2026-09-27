#!/usr/bin/env python3
#
# tests/test_lalal_wait_for_completion.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""wait_for_completion() must ride out transport-level poll failures instead
of abandoning an otherwise-successful split on the first timeout/connection
error, while still failing fast on a definitive LalalError.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from app.lalal import LalalClient, LalalError, LalalProcessingError


class WaitForCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LalalClient("test-key")
        self.addCleanup(lambda: asyncio.run(self.client.close()))

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    @staticmethod
    def _success_payload() -> dict[str, Any]:
        return {
            "status": "success",
            "result": {
                "tracks": [
                    {"type": "stem", "url": "https://example.test/stem.wav", "size": 100},
                    {"type": "back", "url": "https://example.test/back.wav", "size": 200},
                ],
                "duration": 12.5,
            },
        }

    def test_succeeds_immediately_when_the_task_is_already_done(self) -> None:
        with patch.object(
            self.client,
            "check_progress",
            AsyncMock(return_value=self._success_payload()),
        ):
            result = self._run(
                self.client.wait_for_completion("task-1", poll_interval=0.01, timeout=5.0)
            )
        self.assertEqual(result["stem_track"], "https://example.test/stem.wav")
        self.assertEqual(result["back_track"], "https://example.test/back.wav")

    def test_recovers_from_a_transient_timeout_without_failing_the_split(self) -> None:
        responses: list[Any] = [
            TimeoutError("poll timed out"),
            httpx.ConnectTimeout("connect timed out"),
            self._success_payload(),
        ]

        async def fake_check_progress(_task_id: str) -> dict[str, Any]:
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(self.client, "check_progress", fake_check_progress):
            result = self._run(
                self.client.wait_for_completion("task-1", poll_interval=0.01, timeout=5.0)
            )
        self.assertEqual(result["stem_track"], "https://example.test/stem.wav")
        self.assertEqual(responses, [])

    def test_gives_up_after_too_many_consecutive_transient_failures(self) -> None:
        with patch.object(
            self.client,
            "check_progress",
            AsyncMock(side_effect=httpx.ConnectError("unreachable")),
        ):
            with self.assertRaises(LalalProcessingError):
                self._run(
                    self.client.wait_for_completion("task-1", poll_interval=0.001, timeout=5.0)
                )

    def test_a_definitive_lalal_error_still_raises_immediately(self) -> None:
        calls = 0

        async def fake_check_progress(_task_id: str) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise LalalError("Invalid API key")

        with patch.object(self.client, "check_progress", fake_check_progress):
            with self.assertRaises(LalalError):
                self._run(
                    self.client.wait_for_completion("task-1", poll_interval=0.01, timeout=5.0)
                )
        # No retry loop for a definitive rejection: exactly one attempt.
        self.assertEqual(calls, 1)

    def test_a_processing_error_state_still_raises_immediately(self) -> None:
        with patch.object(
            self.client,
            "check_progress",
            AsyncMock(return_value={"status": "error", "error": "boom"}),
        ):
            with self.assertRaises(LalalProcessingError):
                self._run(
                    self.client.wait_for_completion("task-1", poll_interval=0.01, timeout=5.0)
                )


if __name__ == "__main__":
    unittest.main()
