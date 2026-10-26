#!/usr/bin/env python3
#
# tests/test_mp3_tags.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""ID3 tags of served MP3s: cover art, Fetchly comment and BPM (TBPM)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest

from app.audio import tags
from app.audio.hashing import compute_audio_hash
from app.routes import media
from app.runtime.governor import Governor, GovernorConfig


def _patch_governor() -> AbstractContextManager[object]:
    """Swap in a configured governor so the transcode budget exists in tests."""
    fresh = Governor()
    fresh.configure(GovernorConfig(transcode_semaphore_limit=1))
    return patch.object(media, "governor", fresh)


class TagHelperTests(unittest.TestCase):
    def test_bpm_is_rounded_and_unusable_tempo_yields_none(self) -> None:
        self.assertEqual(tags.bpm_to_int(93.6), 94)
        self.assertEqual(tags.bpm_to_int(120), 120)
        for bpm in (None, 0, -3, "94", True):
            with self.subTest(bpm=bpm):
                self.assertIsNone(tags.bpm_to_int(bpm))

    def test_mp3_path_drops_the_source_marker(self) -> None:
        self.assertEqual(tags.mp3_path_for_source(Path("/d/a.source.opus")), Path("/d/a.mp3"))

    def test_sanitize_removes_control_characters(self) -> None:
        self.assertEqual(tags.sanitize_tag_value("a\x00b\nc\x7f d"), "a b c d")


class BuildCommandTests(unittest.TestCase):
    def test_without_cover_no_picture_is_mapped(self) -> None:
        cmd = tags.build_mp3_transcode_cmd(Path("in.opus"), Path("out.mp3"), bpm=None)
        self.assertNotIn("attached_pic", cmd)
        self.assertNotIn("mjpeg", cmd)
        self.assertFalse(any(part.startswith("TBPM=") for part in cmd))
        self.assertIn(f"comment={tags.COMMENT_TEXT}", cmd)

    def test_cover_and_bpm_are_added(self) -> None:
        cmd = tags.build_mp3_transcode_cmd(Path("in.opus"), Path("out.mp3"), cover=Path("c.jpg"), bpm=93.6)
        self.assertIn("attached_pic", cmd)
        self.assertIn("TBPM=94", cmd)
        self.assertEqual(cmd[-1], "out.mp3")

    def test_cover_filter_caps_both_edges_without_upscaling(self) -> None:
        cmd = tags.build_mp3_transcode_cmd(Path("in.opus"), Path("out.mp3"), cover=Path("c.jpg"))
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn(f"w='min(iw,{tags.COVER_MAX_EDGE})'", vf)
        self.assertIn(f"h='min(ih,{tags.COVER_MAX_EDGE})'", vf)
        self.assertIn("force_original_aspect_ratio=decrease", vf)


class FindCoverTests(unittest.TestCase):
    def test_missing_empty_and_present_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self.assertIsNone(tags.find_cover(job_dir))
            (job_dir / tags.COVER_FILENAME).write_bytes(b"")
            self.assertIsNone(tags.find_cover(job_dir))
            (job_dir / tags.COVER_FILENAME).write_bytes(b"jpeg")
            self.assertEqual(tags.find_cover(job_dir), job_dir / tags.COVER_FILENAME)


class EnsureBpmTagTests(unittest.TestCase):
    def test_no_usable_tempo_never_touches_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "a.mp3"
            mp3.write_bytes(b"x")
            with patch.object(tags.subprocess, "run") as run:
                self.assertFalse(tags.ensure_bpm_tag(mp3, None))
                self.assertFalse(tags.ensure_bpm_tag(mp3, 0))
            run.assert_not_called()

    def test_ffmpeg_failure_raises_and_keeps_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "a.mp3"
            mp3.write_bytes(b"original")
            failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"boom")
            with (
                patch.object(tags, "read_bpm_tag", return_value=None),
                patch.object(tags.subprocess, "run", return_value=failed),
                self.assertRaises(RuntimeError),
            ):
                tags.ensure_bpm_tag(mp3, 120)
            self.assertEqual(mp3.read_bytes(), b"original")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["a.mp3"])


class EnsureMp3FailureTests(unittest.TestCase):
    def test_tagging_failure_does_not_fail_serving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.source.opus"
            source.write_bytes(b"src")
            mp3 = Path(tmp) / "a.mp3"
            mp3.write_bytes(b"mp3")

            async def boom(*_args: object, **_kwargs: object) -> bool:
                raise RuntimeError("boom")

            with (
                _patch_governor(),
                patch.object(media, "ensure_bpm_tag_async", side_effect=boom),
            ):
                result = asyncio.run(media._ensure_mp3(source, 120))
            self.assertEqual(result, mp3)
            self.assertEqual(mp3.read_bytes(), b"mp3")


class EnsureBpmTagAsyncCancelTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    async def test_cancellation_kills_ffmpeg_and_cleans_the_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "a.mp3"
            mp3.write_bytes(b"original")

            captured: list[asyncio.subprocess.Process] = []
            started = asyncio.Event()
            real_exec = asyncio.create_subprocess_exec

            async def capturing_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
                proc = await real_exec(*args, **kwargs)
                captured.append(proc)
                started.set()
                return proc

            # A long-running ffmpeg writing to the temp file: it neither finishes
            # nor hits the timeout before the test cancels the await.
            def slow_plan(mp3_path: Path, _bpm: object) -> tuple[int, Path, list[str]]:
                temp_path = mp3_path.with_name(f"{mp3_path.name}.tmp.cancel")
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=30",
                    "-codec:a",
                    "libmp3lame",
                    "-f",
                    "mp3",
                    str(temp_path),
                ]
                return 120, temp_path, cmd

            with (
                patch.object(tags, "_bpm_rewrite_plan", side_effect=slow_plan),
                patch.object(tags.asyncio, "create_subprocess_exec", side_effect=capturing_exec),
            ):
                task = asyncio.create_task(tags.ensure_bpm_tag_async(mp3, 120))
                # Wait until the ffmpeg process is actually running before cancelling.
                await asyncio.wait_for(started.wait(), timeout=10)
                proc = captured[0]
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            # The process was reaped, not left running until the 30 s duration.
            self.assertIsNotNone(proc.returncode)
            # The original is untouched and no temp file survives.
            self.assertEqual(mp3.read_bytes(), b"original")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["a.mp3"])


class _FakeProc:
    """Minimal asyncio subprocess stand-in for the async tagging tests.

    ``communicate`` raises a caller-supplied exception so the async path can be
    driven through timeout and cancellation without a real ffmpeg or a 120 s
    wait; the real ``asyncio.wait_for`` propagates it unchanged.
    """

    def __init__(self, raise_on_communicate: BaseException | None = None) -> None:
        self.returncode: int | None = None
        self.killed = False
        self._raise = raise_on_communicate

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._raise is not None:
            raise self._raise
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class EnsureBpmTagAsyncContractTests(unittest.IsolatedAsyncioTestCase):
    """The async path must stay off the loop and keep the serving contract."""

    async def test_plan_resolution_is_offloaded_to_a_thread(self) -> None:
        plan = (120, Path("/d/a.mp3.tmp"), ["ffmpeg"])
        fake = _FakeProc()
        fake.returncode = 0
        ran_off_loop: list[bool] = []

        def fake_plan(_mp3: Path, _bpm: object) -> tuple[int, Path, list[str]]:
            # In a worker thread there is no running loop; get_running_loop()
            # raising proves the blocking resolver was offloaded, not inlined.
            try:
                asyncio.get_running_loop()
                ran_off_loop.append(False)
            except RuntimeError:
                ran_off_loop.append(True)
            return plan

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
            return fake

        with (
            patch.object(tags, "_bpm_rewrite_plan", side_effect=fake_plan),
            patch.object(tags.asyncio, "create_subprocess_exec", side_effect=fake_exec),
            patch.object(tags, "_finalize_bpm_rewrite") as finalize,
        ):
            result = await tags.ensure_bpm_tag_async(Path("/d/a.mp3"), 120)

        self.assertTrue(result)
        finalize.assert_called_once()
        # The resolver ran in a worker thread, not on the event loop.
        self.assertEqual(ran_off_loop, [True])

    async def test_timeout_is_translated_to_runtimeerror_and_reaps_ffmpeg(self) -> None:
        plan = (120, Path("/d/a.mp3.tmp"), ["ffmpeg"])
        fake = _FakeProc(raise_on_communicate=TimeoutError())

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
            return fake

        with (
            patch.object(tags, "_bpm_rewrite_plan", return_value=plan),
            patch.object(tags.asyncio, "create_subprocess_exec", side_effect=fake_exec),
        ):
            with self.assertRaises(RuntimeError):
                await tags.ensure_bpm_tag_async(Path("/d/a.mp3"), 120)

        # A tagging timeout must surface as RuntimeError, not a bare TimeoutError,
        # and the ffmpeg process must be reaped rather than orphaned.
        self.assertTrue(fake.killed)

    async def test_cancellation_propagates_and_reaps_ffmpeg(self) -> None:
        plan = (120, Path("/d/a.mp3.tmp"), ["ffmpeg"])
        fake = _FakeProc(raise_on_communicate=asyncio.CancelledError())

        async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
            return fake

        with (
            patch.object(tags, "_bpm_rewrite_plan", return_value=plan),
            patch.object(tags.asyncio, "create_subprocess_exec", side_effect=fake_exec),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await tags.ensure_bpm_tag_async(Path("/d/a.mp3"), 120)

        # Cancellation stays a CancelledError (never swallowed or converted) and
        # still terminates the ffmpeg process.
        self.assertTrue(fake.killed)


class EnsureMp3TimeoutServingTests(unittest.TestCase):
    """A tagging timeout must not fail the MP3 download."""

    def test_tagging_timeout_still_returns_the_existing_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.source.opus"
            source.write_bytes(b"src")
            mp3 = Path(tmp) / "a.mp3"
            mp3.write_bytes(b"mp3")

            fake = _FakeProc(raise_on_communicate=TimeoutError())
            plan = (120, mp3.with_name("a.mp3.tmp"), ["ffmpeg"])

            async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
                return fake

            with (
                _patch_governor(),
                patch.object(tags, "_bpm_rewrite_plan", return_value=plan),
                patch.object(tags.asyncio, "create_subprocess_exec", side_effect=fake_exec),
            ):
                result = asyncio.run(media._ensure_mp3(source, 120))

            # RuntimeError from the timeout is caught by _ensure_mp3, so the
            # untagged MP3 is served instead of the request failing.
            self.assertEqual(result, mp3)
            self.assertEqual(mp3.read_bytes(), b"mp3")
            self.assertTrue(fake.killed)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=120)


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream=codec_name,codec_type,width,height:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return json.loads(out.stdout)


@pytest.mark.slow
@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not on PATH")
class RealFfmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        governor_patch = _patch_governor()
        governor_patch.start()
        self.addCleanup(governor_patch.stop)
        self.dir = Path(self._tmp.name)
        self.source = self.dir / "Track.source.wav"
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(self.source))

    def _add_cover(self) -> None:
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=1920x1080",
            "-frames:v",
            "1",
            str(self.dir / tags.COVER_FILENAME),
        )

    def _cover_size_after_transcode(self, width: int, height: int) -> tuple[int, int]:
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s={width}x{height}",
            "-frames:v",
            "1",
            str(self.dir / tags.COVER_FILENAME),
        )
        mp3 = media.get_mp3_cache_path(self.source)
        asyncio.run(media.transcode_to_mp3(self.source, mp3, fetchly_tags=True, bpm=None))
        pics = [s for s in _probe(mp3)["streams"] if s["disposition"]["attached_pic"] == 1]
        self.assertEqual(len(pics), 1)
        return pics[0]["width"], pics[0]["height"]

    def test_portrait_cover_is_bounded_on_both_edges(self) -> None:
        width, height = self._cover_size_after_transcode(1080, 1920)
        self.assertLessEqual(max(width, height), tags.COVER_MAX_EDGE)
        self.assertEqual(height, tags.COVER_MAX_EDGE)
        self.assertAlmostEqual(width / height, 1080 / 1920, delta=0.01)

    def test_landscape_cover_is_bounded_on_both_edges(self) -> None:
        width, height = self._cover_size_after_transcode(1920, 1080)
        self.assertLessEqual(max(width, height), tags.COVER_MAX_EDGE)
        self.assertEqual(width, tags.COVER_MAX_EDGE)
        self.assertAlmostEqual(width / height, 1920 / 1080, delta=0.01)

    def test_small_cover_is_not_upscaled(self) -> None:
        self.assertEqual(self._cover_size_after_transcode(640, 480), (640, 480))

    @staticmethod
    def _tag(info: dict, key: str) -> str | None:
        found = {k.lower(): v for k, v in (info["format"].get("tags") or {}).items()}
        return found.get(key.lower())

    def test_cover_comment_and_bpm_end_to_end(self) -> None:
        self._add_cover()
        mp3 = media.get_mp3_cache_path(self.source)
        hash_before = compute_audio_hash(self.source)

        asyncio.run(media.transcode_to_mp3(self.source, mp3, fetchly_tags=True, bpm=None))
        info = _probe(mp3)
        self.assertIsNone(self._tag(info, "TBPM"))
        self.assertIn(tags.COMMENT_TEXT, self._tag(info, "comment") or "")
        pics = [s for s in info["streams"] if s["disposition"]["attached_pic"] == 1]
        self.assertEqual([s["codec_name"] for s in pics], ["mjpeg"])

        # The tempo arrives after the first MP3 exists: it is added in place.
        served = asyncio.run(media._ensure_mp3(self.source, 120.4))
        self.assertEqual(served, mp3)
        info = _probe(mp3)
        self.assertEqual(self._tag(info, "TBPM"), "120")
        self.assertIn(tags.COMMENT_TEXT, self._tag(info, "comment") or "")
        self.assertEqual(sum(s["disposition"]["attached_pic"] for s in info["streams"]), 1)
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["Track.mp3", "Track.source.wav", tags.COVER_FILENAME],
        )
        self.assertEqual(compute_audio_hash(self.source), hash_before)
        self.assertGreaterEqual(mp3.stat().st_mtime, self.source.stat().st_mtime)

        # Same tempo again is a no-op; the file is not rewritten.
        self.assertFalse(tags.ensure_bpm_tag(mp3, 120))

    def test_async_rewrite_sets_bpm_and_keeps_cover_and_comment(self) -> None:
        self._add_cover()
        mp3 = media.get_mp3_cache_path(self.source)
        asyncio.run(media.transcode_to_mp3(self.source, mp3, fetchly_tags=True, bpm=None))

        rewritten = asyncio.run(tags.ensure_bpm_tag_async(mp3, 128.4))
        self.assertTrue(rewritten)
        info = _probe(mp3)
        self.assertEqual(self._tag(info, "TBPM"), "128")
        self.assertIn(tags.COMMENT_TEXT, self._tag(info, "comment") or "")
        self.assertEqual(sum(s["disposition"]["attached_pic"] for s in info["streams"]), 1)
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["Track.mp3", "Track.source.wav", tags.COVER_FILENAME],
        )
        # No usable tempo is a no-op, same contract as the sync variant.
        self.assertFalse(asyncio.run(tags.ensure_bpm_tag_async(mp3, None)))

    def test_no_thumbnail_still_produces_a_tagged_mp3(self) -> None:
        mp3 = media.get_mp3_cache_path(self.source)
        asyncio.run(media.transcode_to_mp3(self.source, mp3, fetchly_tags=True, bpm=93.6))
        info = _probe(mp3)
        self.assertEqual(self._tag(info, "TBPM"), "94")
        self.assertEqual([s for s in info["streams"] if s["disposition"]["attached_pic"] == 1], [])

    def test_broken_cover_falls_back_to_a_cover_free_mp3(self) -> None:
        (self.dir / tags.COVER_FILENAME).write_bytes(b"not a jpeg")
        mp3 = media.get_mp3_cache_path(self.source)
        asyncio.run(media.transcode_to_mp3(self.source, mp3, fetchly_tags=True, bpm=None))
        info = _probe(mp3)
        self.assertIn(tags.COMMENT_TEXT, self._tag(info, "comment") or "")
        self.assertEqual([s for s in info["streams"] if s["disposition"]["attached_pic"] == 1], [])

    def test_ffprobe_reads_untagged_files_as_no_bpm(self) -> None:
        plain = self.dir / "plain.mp3"
        _ffmpeg("-i", str(self.source), str(plain))
        self.assertIsNone(tags.read_bpm_tag(plain))


if __name__ == "__main__":
    unittest.main()
