#!/usr/bin/env python3
#
# tests/test_lalal_route_safety.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import tempfile
import unittest
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.integrations.lalal.client import LalalError, LalalUnavailableError
from app.routes import lalal


class LalalRouteSafetyTests(unittest.TestCase):
    def test_capacity_exhaustion_returns_retryable_error(self) -> None:
        async def check() -> HTTPException:
            original_capacity = lalal._processing_capacity
            try:
                lalal._processing_capacity = asyncio.Semaphore(0)
                with patch.object(lalal, "_LALAL_CAPACITY_WAIT_SECONDS", 0.01):
                    with self.assertRaises(HTTPException) as raised:
                        async with lalal._processing_capacity_slot():
                            self.fail("unreachable")
                return raised.exception
            finally:
                lalal._processing_capacity = original_capacity

        error = asyncio.run(check())
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.headers, {"Retry-After": "5"})

    def test_capacity_slot_is_released_after_processing(self) -> None:
        async def check() -> None:
            original_capacity = lalal._processing_capacity
            try:
                capacity = asyncio.Semaphore(1)
                lalal._processing_capacity = capacity
                async with lalal._processing_capacity_slot():
                    self.assertTrue(capacity.locked())
                self.assertFalse(capacity.locked())
            finally:
                lalal._processing_capacity = original_capacity

        asyncio.run(check())

    def test_processing_attempt_cleans_outputs_before_releasing_lock(self) -> None:
        async def check(lock_file: Path, output_path: Path) -> None:
            with self.assertRaisesRegex(RuntimeError, "split failed"):
                async with lalal._processing_attempt(lock_file, output_path):
                    raise RuntimeError("split failed")
            self.assertFalse(output_path.exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "vocals.mp3"
            output_path.write_bytes(b"partial")
            asyncio.run(check(root / ".lalal.lock", output_path))

    def test_mp3_stem_is_published_by_atomic_rename(self) -> None:
        async def check(source: Path, target: Path) -> None:
            await lalal._finalize_stem(source, target)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "returned.mp3"
            target = root / "vocals.mp3"
            source.write_bytes(b"complete stem")

            asyncio.run(check(source, target))

            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"complete stem")


class LalalCredentialValidationTests(unittest.TestCase):
    def _activate(self, error: BaseException) -> HTTPException:
        body = lalal.ActivationKeyRequest(email="a@example.com", activation_key="key")
        with patch("app.integrations.lalal.client.LalalClient.check_quota", AsyncMock(side_effect=error)):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(lalal.api_lalal_auth_activation_key(body, _user="admin"))
        return raised.exception

    def test_transient_errors_never_report_an_invalid_key(self) -> None:
        for error in (LalalUnavailableError("HTTP 429"), httpx.ConnectError("down")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self._activate(error).status_code, 503)

    def test_rejected_key_and_timeout_keep_their_status(self) -> None:
        self.assertEqual(self._activate(LalalError("Invalid API key")).status_code, 400)
        self.assertEqual(self._activate(TimeoutError()).status_code, 504)

    def test_status_keeps_the_last_verdict_on_a_transient_error(self) -> None:
        settings = {
            "lalalaai_email": "a@example.com",
            "lalalaai_auth_key": "key",
            "lalalaai_auth_checked_at": 0,
            "lalalaai_auth_is_valid": True,
            "lalalaai_minutes_left": 12,
        }
        for error, expected_valid in (
            (LalalUnavailableError("HTTP 503"), True),
            (LalalError("Invalid API key"), False),
        ):
            with (
                self.subTest(error=str(error)),
                patch.object(lalal, "get_settings", return_value=settings),
                patch.object(lalal, "_commit_auth_settings", AsyncMock(return_value=True)),
                patch("app.integrations.lalal.client.LalalClient.check_quota", AsyncMock(side_effect=error)),
            ):
                result = asyncio.run(lalal.api_lalal_status(force_refresh=True, _user="admin"))
                self.assertEqual(result.token_valid, expected_valid)


class LalalDurationGuardTests(unittest.TestCase):
    def _split(self, duration: object, *, guard: bool) -> str:
        """Return "blocked" on the guard's 400, "passed" once the route moved on."""
        job = {"status": "done", "type": "audio", "filename": "song.mp3", "bpm": None, "duration_seconds": duration}
        settings = {"lalalaai_auth_key": "key", "lalalaai_duration_guard": guard}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "song.mp3"
            source.write_bytes(b"x")
            with (
                patch.object(lalal, "_require_initialized"),
                patch.object(lalal, "get_job", return_value=job),
                patch.object(lalal, "job_dir", return_value=Path(tmp)),
                patch.object(lalal, "resolve_job_path", return_value=source),
                patch.object(lalal, "get_settings", return_value=settings),
                patch.object(lalal, "_get_cached_split_response", AsyncMock(return_value=None)),
                patch.object(lalal, "_queue_event", None),
            ):
                try:
                    asyncio.run(lalal.lalal_split(uuid.uuid4(), _user="admin"))
                except HTTPException as exc:
                    self.assertEqual(exc.status_code, 400)
                    return "blocked"
                except RuntimeError:
                    # First statement after the guard: the route is not wired up in this test.
                    return "passed"
        raise AssertionError("route returned without reaching either outcome")

    def test_guard_on_blocks_only_tracks_over_the_limit(self) -> None:
        for duration, outcome in ((599, "passed"), (600, "passed"), (601, "blocked")):
            with self.subTest(duration=duration):
                self.assertEqual(self._split(duration, guard=True), outcome)

    def test_guard_on_blocks_unknown_or_invalid_duration(self) -> None:
        for duration in (None, "n/a", float("nan"), True):
            with self.subTest(duration=duration):
                self.assertEqual(self._split(duration, guard=True), "blocked")

    def test_guard_off_lets_long_and_unknown_tracks_through(self) -> None:
        for duration in (601, 5000, None):
            with self.subTest(duration=duration):
                self.assertEqual(self._split(duration, guard=False), "passed")


class LalalFinalizeCleanupTests(unittest.TestCase):
    """A failing transcode after process_file must not strand WAV intermediates."""

    def _run(self, fail_on: str, error: BaseException) -> tuple[list[str], BaseException | None]:
        """Run the route with `fail_on` ("stem"/"backing") failing; return (leftover names, raised)."""
        job = {"status": "done", "type": "audio", "filename": "song.mp3", "bpm": None, "duration_seconds": 100}
        settings = {"lalalaai_auth_key": "key", "lalalaai_duration_guard": True}

        class FakeClient:
            def __init__(self, _key: str) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def process_file(self, _src: Path, output_dir: Path, **_kwargs: object) -> dict[str, Path]:
                stem, backing = output_dir / "song_vocals.wav", output_dir / "song_backing.wav"
                stem.write_bytes(b"s")
                backing.write_bytes(b"b")
                return {"stem": stem, "backing": backing}

        async def fake_transcode(source: Path, target: Path) -> None:
            if source.name == f"song_{'vocals' if fail_on == 'stem' else 'backing'}.wav":
                raise error
            target.write_bytes(b"mp3")

        @asynccontextmanager
        async def slot() -> AsyncIterator[None]:
            yield

        raised: BaseException | None = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "song.mp3"
            source.write_bytes(b"x")
            (root / "other_vocals.mp3").write_bytes(b"cached")
            with (
                patch.object(lalal, "_require_initialized"),
                patch.object(lalal, "get_job", return_value=job),
                patch.object(lalal, "job_dir", return_value=root),
                patch.object(lalal, "resolve_job_path", return_value=source),
                patch.object(lalal, "get_settings", return_value=settings),
                patch.object(lalal, "_get_cached_split_response", AsyncMock(return_value=None)),
                patch.object(lalal, "_queue_event", lambda _payload: None),
                patch.object(lalal, "_processing_capacity_slot", slot),
                patch.object(lalal, "_decode_for_upload", AsyncMock(return_value=(source, None))),
                patch.object(lalal, "transcode_to_mp3", fake_transcode),
                patch("app.integrations.lalal.client.LalalClient", FakeClient),
            ):
                try:
                    asyncio.run(lalal.lalal_split(uuid.uuid4(), _user="admin"))
                except BaseException as exc:  # noqa: BLE001 - the exception type is the assertion
                    raised = exc
            leftovers = sorted(p.name for p in root.iterdir() if not p.name.endswith(".lock"))
        return leftovers, raised

    def test_first_transcode_failure_removes_wav_intermediates(self) -> None:
        leftovers, raised = self._run("stem", RuntimeError("ffmpeg failed"))
        self.assertIsInstance(raised, HTTPException)
        self.assertEqual(getattr(raised, "status_code", None), 500)
        self.assertEqual(leftovers, ["other_vocals.mp3", "song.mp3"])

    def test_second_transcode_failure_leaves_no_intermediates_or_half_pair(self) -> None:
        leftovers, raised = self._run("backing", RuntimeError("ffmpeg failed"))
        self.assertIsInstance(raised, HTTPException)
        self.assertEqual(getattr(raised, "status_code", None), 500)
        # The first stem's MP3 is removed by _processing_attempt, so no half pair stays.
        self.assertEqual(leftovers, ["other_vocals.mp3", "song.mp3"])

    def test_unrelated_cached_mp3_survives_failure(self) -> None:
        leftovers, _raised = self._run("stem", RuntimeError("ffmpeg failed"))
        self.assertIn("other_vocals.mp3", leftovers)

    def test_cancellation_during_finalize_still_cleans_up(self) -> None:
        leftovers, raised = self._run("backing", asyncio.CancelledError())
        self.assertIsInstance(raised, asyncio.CancelledError)
        self.assertEqual(leftovers, ["other_vocals.mp3", "song.mp3"])


if __name__ == "__main__":
    unittest.main()
