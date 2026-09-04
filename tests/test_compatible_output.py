#!/usr/bin/env python3
#
# tests/test_compatible_output.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The "Universally playable output" promise and the one pass that keeps it.

The promise is meant to cost nothing in the common case: yt-dlp is asked to
pick an H.264/AAC rendition, and only a source that has none reaches an
encoder. These tests pin that down, because the whole point of the setting is
CPU and quality *not* spent.
"""

import threading
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from app import worker
from app.utils.watermark import VideoWatermark


class CompatibleOutputRequiredTests(unittest.TestCase):
    def test_the_users_choice_decides_when_the_watermark_is_off(self):
        self.assertTrue(
            worker._compatible_output_required(
                {"video_watermark": False, "download_compatible_output": True}
            )
        )
        self.assertFalse(
            worker._compatible_output_required(
                {"video_watermark": False, "download_compatible_output": False}
            )
        )

    def test_the_watermark_implies_the_promise(self):
        # That pass runs libx264 either way, so the compatible container is free.
        self.assertTrue(
            worker._compatible_output_required(
                {"video_watermark": True, "download_compatible_output": False}
            )
        )

    def test_defaults_match_the_settings_store(self):
        from app.db import _SETTINGS_DEFAULTS

        self.assertEqual(_SETTINGS_DEFAULTS["download_compatible_output"], "false")
        self.assertEqual(
            worker._DEFAULT_COMPATIBLE_OUTPUT,
            _SETTINGS_DEFAULTS["download_compatible_output"] == "true",
        )
        # Watermark on by default, so a stock install still gets H.264/AAC.
        self.assertEqual(_SETTINGS_DEFAULTS["video_watermark"], "true")


class MaxQualityFormatSelectionTests(unittest.TestCase):
    def _cmd(self, *, compatible: bool) -> list[str]:
        with patch.object(worker, "_download_tuning", return_value=(3, compatible)):
            return worker._build_ytdlp_cmd(
                "https://example.com/watch?v=x",
                "/tmp/out.%(ext)s",
                media_type="video",
                quality="max",
            )

    def test_the_promise_is_kept_by_sorting_not_by_re_encoding(self):
        cmd = self._cmd(compatible=True)
        self.assertIn("-S", cmd)
        sort = cmd[cmd.index("-S") + 1]
        # vcodec ahead of res: a 1080p H.264 rendition beats a 2160p AV1 one.
        self.assertLess(sort.index("vcodec:h264"), sort.index("res"))
        self.assertIn("acodec:aac", sort)
        self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")

    def test_without_the_promise_the_container_is_not_forced(self):
        cmd = self._cmd(compatible=False)
        self.assertNotIn("--merge-output-format", cmd)
        self.assertNotIn("-S", cmd)
        self.assertNotIn("--remux-video", cmd)
        # Still best video plus best audio - only the codec preference is gone.
        self.assertEqual(cmd[cmd.index("-f") + 1], "bv*+ba/b")

    def test_the_capped_qualities_are_unaffected(self):
        # They always re-encode to H.264/AAC, so the promise is moot there.
        with patch.object(worker, "_download_tuning", return_value=(3, False)):
            cmd = worker._build_ytdlp_cmd(
                "https://example.com/watch?v=x",
                "/tmp/out.%(ext)s",
                media_type="video",
                quality="medium",
            )
        self.assertEqual(cmd[cmd.index("-f") + 1], "bv*[height<=720]+ba/b")
        self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")


class FinalizeVideoDownloadTests(unittest.TestCase):
    """Which pass, if any, runs after a "max" download."""

    def setUp(self):
        self.badge = VideoWatermark(path=Path("/tmp/badge.png"), width=179, height=67, margin=18)

    def _run(self, *, compatible, watermark, codecs, suffix=".mp4"):
        """Return the ffmpeg argv the finalize step would run, or None."""
        video = Path(f"/tmp/job/Title (maxQuality){suffix}")
        captured: list[list[str]] = []

        def fake_transcode(cmd, **_kwargs):
            captured.append(cmd)

        with (
            # The governor is configured at application startup, not in a unit test.
            patch.object(
                type(worker.governor),
                "transcode_semaphore_sync",
                new_callable=PropertyMock,
                return_value=threading.Semaphore(1),
            ),
            patch.object(worker, "_probe_video_size", return_value=(1920, 1080)),
            patch.object(worker, "_resolve_watermark", return_value=self.badge if watermark else None),
            patch.object(worker, "_stream_codecs", return_value=codecs),
            patch.object(worker, "_probe_media", return_value=("h264", 3000, 120.0)),
            patch.object(worker, "_transition_worker_status"),
            patch.object(worker, "_run_ffmpeg_transcode", side_effect=fake_transcode),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "stat"),
            patch.object(Path, "replace"),
            patch.object(Path, "unlink"),
        ):
            worker._finalize_video_download(
                "job-1", video, compatible_output=compatible, transcode_timeout_seconds=60
            )
        return captured[0] if captured else None

    def test_an_already_compatible_file_is_never_touched(self):
        # The common case: the H.264/AAC rendition was picked at download time.
        self.assertIsNone(self._run(compatible=True, watermark=False, codecs=("h264", "aac")))

    def test_without_the_promise_and_without_a_watermark_nothing_runs(self):
        self.assertIsNone(self._run(compatible=False, watermark=False, codecs=("av1", "opus")))

    def test_an_incompatible_video_codec_is_re_encoded(self):
        cmd = self._run(compatible=True, watermark=False, codecs=("av01", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        # Audio was already fine, so it is not touched.
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_only_incompatible_audio_leaves_the_video_untouched(self):
        # The expensive half of the work is skipped: this is a stream copy.
        cmd = self._run(compatible=True, watermark=False, codecs=("h264", "opus"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_the_watermark_and_the_conversion_share_one_pass(self):
        cmd = self._run(compatible=True, watermark=True, codecs=("av01", "opus"))
        assert cmd is not None
        self.assertEqual(cmd.count("-i"), 2)  # video plus badge, one ffmpeg run
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("-filter_complex", cmd)

    def test_a_watermark_on_a_compatible_file_still_only_encodes_video(self):
        cmd = self._run(compatible=True, watermark=True, codecs=("h264", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_a_file_without_audio_needs_no_audio_work(self):
        cmd = self._run(compatible=True, watermark=True, codecs=("h264", ""))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_an_unreadable_codec_is_treated_as_incompatible(self):
        # Better a needless encode than a file that silently will not play.
        cmd = self._run(compatible=True, watermark=False, codecs=("", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")

    def test_the_promise_moves_the_output_into_mp4(self):
        cmd = self._run(compatible=True, watermark=False, codecs=("vp9", "opus"), suffix=".webm")
        assert cmd is not None
        self.assertTrue(cmd[-1].endswith(".mp4"))


if __name__ == "__main__":
    unittest.main()
