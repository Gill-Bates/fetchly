#!/usr/bin/env python3
#
# tests/test_output_modes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The three output modes and the one pass that delivers them.

"source" passes the download through untouched, "universal" guarantees
H.264/AAC in MP4 and "av1" guarantees AV1 video. Both promises are meant to
cost nothing in the common case: yt-dlp is asked to pick a rendition that
already matches, and only a source that has none reaches an encoder. These
tests pin that down, because the whole point of the setting is CPU and quality
*not* spent.
"""

import threading
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from app import worker
from app.db import DOWNLOAD_OUTPUT_MODES
from app.utils.watermark import VideoWatermark


class OutputModeResolutionTests(unittest.TestCase):
    def test_the_stored_mode_is_used_verbatim(self):
        for mode in DOWNLOAD_OUTPUT_MODES:
            for watermark in (False, True):
                with self.subTest(mode=mode, watermark=watermark):
                    self.assertEqual(
                        worker._output_mode(
                            {"video_watermark": watermark, "download_output_mode": mode}
                        ),
                        mode,
                    )

    def test_the_watermark_never_overrides_the_stored_mode(self):
        # The overlay costs a video encode either way, but that encode targets
        # the mode's own codec - for "source" the codec it arrived in - so
        # enabling the watermark must not turn a passthrough into H.264.
        self.assertEqual(
            worker._output_mode({"video_watermark": True, "download_output_mode": "source"}),
            "source",
        )

    def test_an_unknown_mode_falls_back_to_the_default(self):
        self.assertEqual(
            worker._output_mode(
                {"video_watermark": False, "download_output_mode": "vp9-please"}
            ),
            worker._DEFAULT_OUTPUT_MODE,
        )


class TargetVideoCodecTests(unittest.TestCase):
    """Which codec the finished file must carry, per mode."""

    def test_the_targeted_modes_name_their_codec_outright(self):
        for mode, expected in (("universal", "h264"), ("av1", "av1")):
            for source in ("h264", "vp9", "av1", "hevc", "", "prores"):
                with self.subTest(mode=mode, source=source):
                    self.assertEqual(worker._target_video_codec(mode, source), expected)

    def test_source_mode_keeps_every_codec_it_can_re_encode(self):
        for source, expected in (
            ("h264", "h264"),
            ("vp9", "vp9"),
            ("vp8", "vp8"),
            ("av1", "av1"),
            ("av01", "av1"),
            ("hevc", "hevc"),
            ("h265", "hevc"),
        ):
            with self.subTest(source=source):
                self.assertEqual(worker._target_video_codec("source", source), expected)

    def test_source_mode_falls_back_to_h264_without_a_matching_encoder(self):
        for source in ("", "prores", "mpeg2video"):
            with self.subTest(source=source):
                self.assertEqual(worker._target_video_codec("source", source), "h264")

    def test_every_recipe_is_reachable_and_complete(self):
        for codec, recipe in worker._ENCODER_RECIPES.items():
            with self.subTest(codec=codec):
                self.assertTrue(recipe.encoder)
                self.assertIn(recipe.speed_flag, ("-preset", "-cpu-used"))
                # libvpx ignores -crf unless the bitrate target is zeroed.
                if recipe.speed_flag == "-cpu-used":
                    self.assertIn("-b:v", recipe.extra)
                    self.assertEqual(recipe.extra[recipe.extra.index("-b:v") + 1], "0")
                speed, crf = recipe.settings_for(1080)
                self.assertTrue(crf.isdigit())
                self.assertTrue(speed)

    def test_defaults_match_the_settings_store(self):
        from app.db import _SETTINGS_DEFAULTS

        self.assertEqual(_SETTINGS_DEFAULTS["download_output_mode"], worker._DEFAULT_OUTPUT_MODE)
        self.assertIn(worker._DEFAULT_OUTPUT_MODE, DOWNLOAD_OUTPUT_MODES)
        # A stock install produces H.264/AAC: the default mode says so outright,
        # and the watermark would have forced it regardless.
        self.assertEqual(_SETTINGS_DEFAULTS["download_output_mode"], "universal")
        self.assertEqual(_SETTINGS_DEFAULTS["video_watermark"], "true")

    def test_the_worker_and_the_database_agree_on_the_vocabulary(self):
        self.assertEqual(
            set(DOWNLOAD_OUTPUT_MODES),
            {worker._OUTPUT_MODE_SOURCE, worker._OUTPUT_MODE_UNIVERSAL, worker._OUTPUT_MODE_AV1},
        )


class MaxQualityFormatSelectionTests(unittest.TestCase):
    def _cmd(self, *, mode: str, quality: str = "max") -> list[str]:
        with patch.object(worker, "_download_tuning", return_value=(3, mode)):
            return worker._build_ytdlp_cmd(
                "https://example.com/watch?v=x",
                "/tmp/out.%(ext)s",
                media_type="video",
                quality=quality,
            )

    def test_universal_is_kept_by_sorting_not_by_re_encoding(self):
        cmd = self._cmd(mode="universal")
        sort = cmd[cmd.index("-S") + 1]
        # vcodec ahead of res: a 1080p H.264 rendition beats a 2160p AV1 one.
        self.assertLess(sort.index("vcodec:h264"), sort.index("res"))
        self.assertIn("acodec:aac", sort)
        self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")

    def test_av1_prefers_an_av1_rendition_without_forcing_a_container(self):
        cmd = self._cmd(mode="av1")
        sort = cmd[cmd.index("-S") + 1]
        self.assertLess(sort.index("vcodec:av01"), sort.index("res"))
        # AV1 is at home in .webm and .mkv too, so yt-dlp keeps its own choice.
        self.assertNotIn("--merge-output-format", cmd)
        self.assertNotIn("--remux-video", cmd)

    def test_source_forces_nothing_at_all(self):
        cmd = self._cmd(mode="source")
        self.assertNotIn("--merge-output-format", cmd)
        self.assertNotIn("-S", cmd)
        self.assertNotIn("--remux-video", cmd)
        # Still best video plus best audio - only the codec preference is gone.
        self.assertEqual(cmd[cmd.index("-f") + 1], "bv*+ba/b")

    def test_the_capped_qualities_are_unaffected(self):
        # They always re-encode to H.264/AAC, so the mode is moot there.
        for mode in DOWNLOAD_OUTPUT_MODES:
            with self.subTest(mode=mode):
                cmd = self._cmd(mode=mode, quality="medium")
                self.assertEqual(cmd[cmd.index("-f") + 1], "bv*[height<=720]+ba/b")
                self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")


class FinalizeVideoDownloadTests(unittest.TestCase):
    """Which pass, if any, runs after a "max" download."""

    def setUp(self):
        self.badge = VideoWatermark(path=Path("/tmp/badge.png"), width=179, height=67, margin=18)

    def _run(self, *, mode, watermark, codecs, suffix=".mp4"):
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
                "job-1", video, output_mode=mode, transcode_timeout_seconds=60
            )
        return captured[0] if captured else None

    def test_an_already_universal_file_is_never_touched(self):
        # The common case: the H.264/AAC rendition was picked at download time.
        self.assertIsNone(self._run(mode="universal", watermark=False, codecs=("h264", "aac")))

    def test_source_mode_without_a_watermark_never_post_processes(self):
        self.assertIsNone(self._run(mode="source", watermark=False, codecs=("av1", "opus")))

    def test_source_mode_with_a_watermark_re_encodes_into_the_source_codec(self):
        # The overlay forces an encode, but the mode's promise survives it: the
        # file keeps the codec and the container it arrived in.
        for codecs, suffix, encoder in (
            (("vp9", "opus"), ".webm", "libvpx-vp9"),
            (("av1", "opus"), ".webm", "libsvtav1"),
            (("vp8", "vorbis"), ".webm", "libvpx"),
            (("hevc", "aac"), ".mkv", "libx265"),
            (("h264", "aac"), ".mp4", "libx264"),
        ):
            with self.subTest(codec=codecs[0]):
                cmd = self._run(mode="source", watermark=True, codecs=codecs, suffix=suffix)
                assert cmd is not None
                self.assertEqual(cmd[cmd.index("-c:v") + 1], encoder)
                # Audio is never part of the source promise's work.
                self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
                self.assertTrue(cmd[-1].endswith(suffix), f"{cmd[-1]} should stay {suffix}")
                self.assertIn("-filter_complex", cmd)

    def test_the_libvpx_encoders_zero_the_bitrate_so_crf_applies(self):
        cmd = self._run(mode="source", watermark=True, codecs=("vp9", "opus"), suffix=".webm")
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "0")
        self.assertTrue(cmd[cmd.index("-cpu-used") + 1].isdigit())

    def test_source_mode_falls_back_to_h264_in_mp4_for_an_unencodable_codec(self):
        # No libprores encoder here, so the overlay pass cannot keep the codec;
        # the container has to move too, since .mkv-only sources may not accept
        # what we produce.
        cmd = self._run(mode="source", watermark=True, codecs=("prores", "pcm_s16le"), suffix=".mov")
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertTrue(cmd[-1].endswith(".mp4"))

    def test_an_incompatible_video_codec_is_re_encoded(self):
        cmd = self._run(mode="universal", watermark=False, codecs=("av01", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        # Audio was already fine, so it is not touched.
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_only_incompatible_audio_leaves_the_video_untouched(self):
        # The expensive half of the work is skipped: this is a stream copy.
        cmd = self._run(mode="universal", watermark=False, codecs=("h264", "opus"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_an_audio_only_fix_still_lands_in_mp4(self):
        # AAC cannot stay in a .webm, so the container moves even though the
        # video is only copied.
        cmd = self._run(mode="universal", watermark=False, codecs=("h264", "opus"), suffix=".webm")
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertTrue(cmd[-1].endswith(".mp4"))

    def test_the_watermark_and_the_conversion_share_one_pass(self):
        cmd = self._run(mode="universal", watermark=True, codecs=("av01", "opus"))
        assert cmd is not None
        self.assertEqual(cmd.count("-i"), 2)  # video plus badge, one ffmpeg run
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("-filter_complex", cmd)

    def test_a_watermark_on_a_compatible_file_still_only_encodes_video(self):
        cmd = self._run(mode="universal", watermark=True, codecs=("h264", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_a_file_without_audio_needs_no_audio_work(self):
        cmd = self._run(mode="universal", watermark=True, codecs=("h264", ""))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_an_unreadable_codec_is_treated_as_off_target(self):
        # Better a needless encode than a file that silently will not play.
        cmd = self._run(mode="universal", watermark=False, codecs=("", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")

    def test_universal_moves_the_output_into_mp4(self):
        cmd = self._run(mode="universal", watermark=False, codecs=("vp9", "opus"), suffix=".webm")
        assert cmd is not None
        self.assertTrue(cmd[-1].endswith(".mp4"))

    def test_an_already_av1_file_is_never_touched(self):
        for codec in ("av1", "av01"):
            with self.subTest(codec=codec):
                self.assertIsNone(
                    self._run(mode="av1", watermark=False, codecs=(codec, "opus"), suffix=".webm")
                )

    def test_a_non_av1_source_is_encoded_with_libsvtav1(self):
        cmd = self._run(mode="av1", watermark=False, codecs=("h264", "aac"))
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libsvtav1")
        # AV1 targets the picture only: a usable audio track is left alone.
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_av1_keeps_the_source_container(self):
        # AV1 is valid in .webm, so there is no reason to rewrap - and rewrapping
        # would drag the Opus track into MP4, which barely tolerates it.
        cmd = self._run(mode="av1", watermark=False, codecs=("vp9", "opus"), suffix=".webm")
        assert cmd is not None
        self.assertTrue(cmd[-1].endswith(".webm"))

    def test_a_watermark_in_av1_mode_encodes_av1_not_h264(self):
        cmd = self._run(mode="av1", watermark=True, codecs=("av1", "opus"), suffix=".webm")
        assert cmd is not None
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libsvtav1")
        self.assertIn("-filter_complex", cmd)

    def test_the_av1_encoder_gets_a_preset_and_a_crf(self):
        cmd = self._run(mode="av1", watermark=False, codecs=("h264", "aac"))
        assert cmd is not None
        # libsvtav1's preset is a number; a non-numeric x264 preset name here
        # would make ffmpeg fail at runtime.
        self.assertTrue(cmd[cmd.index("-preset") + 1].isdigit())
        self.assertTrue(cmd[cmd.index("-crf") + 1].isdigit())


if __name__ == "__main__":
    unittest.main()
