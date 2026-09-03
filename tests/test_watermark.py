#!/usr/bin/env python3
#
# tests/test_watermark.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-watermark-secret")

from app.utils import watermark
from app.utils.watermark import (
    VideoWatermark,
    badge_geometry,
    build_watermark,
    video_filter_args,
)
from app.worker import _scaled_size

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class BadgeGeometryTests(unittest.TestCase):
    def test_width_scales_with_the_video_and_stays_clamped(self):
        self.assertEqual(badge_geometry(1280, 720, "").logo_width, 152)
        # 320x240 and 4K/8K all stay at the same ~12% proportion; only a
        # thumbnail-sized source is small enough to hit the lower bound.
        self.assertEqual(badge_geometry(320, 240, "").logo_width, 40)
        self.assertEqual(badge_geometry(200, 150, "").logo_width, 32)
        self.assertEqual(badge_geometry(3840, 2160, "").logo_width, 464)
        self.assertEqual(badge_geometry(7680, 4320, "").logo_width, 920)
        # And a canvas far beyond any real video still gets capped.
        self.assertEqual(badge_geometry(16_000, 9_000, "").logo_width, 960)

    def test_logo_keeps_its_aspect_ratio(self):
        geometry = badge_geometry(1280, 720, "")
        self.assertEqual(geometry.logo_height, round(geometry.logo_width / (203.56738 / 58.475399)))

    def test_without_text_the_canvas_is_just_the_logo(self):
        geometry = badge_geometry(1280, 720, "")
        self.assertEqual(geometry.font_size, 0)
        self.assertEqual(geometry.canvas_width, geometry.logo_width + geometry.shadow_offset)
        self.assertEqual(geometry.canvas_height, geometry.logo_height + geometry.shadow_offset)

    def test_text_adds_a_line_below_the_logo(self):
        plain = badge_geometry(1280, 720, "")
        with_host = badge_geometry(1280, 720, "fetchly.example.com")
        self.assertGreater(with_host.font_size, 0)
        self.assertGreater(with_host.canvas_height, plain.canvas_height)
        self.assertGreaterEqual(with_host.text_y, with_host.logo_height)

    def test_a_long_hostname_shrinks_the_type_instead_of_the_badge(self):
        short = badge_geometry(1280, 720, "f.example.com")
        long_host = badge_geometry(1280, 720, "media.my-very-long-instance-name.example.org")
        self.assertLess(long_host.font_size, short.font_size)
        self.assertGreaterEqual(long_host.font_size, 8)

    def test_logo_and_text_are_both_flush_right(self):
        geometry = badge_geometry(1280, 720, "fetchly.example.com")
        self.assertEqual(
            geometry.logo_x + geometry.logo_width + geometry.shadow_offset,
            geometry.canvas_width,
        )

    def test_margin_follows_the_video_height(self):
        self.assertEqual(badge_geometry(1280, 720, "").margin, 18)
        # Small formats keep a usable minimum inset.
        self.assertEqual(badge_geometry(320, 180, "").margin, 8)

    def test_shadow_scales_with_the_logo_and_stays_a_soft_fraction(self):
        small = badge_geometry(320, 240, "")
        large = badge_geometry(7680, 4320, "")
        self.assertGreater(large.shadow_offset, small.shadow_offset)
        self.assertGreater(large.shadow_blur, small.shadow_blur)
        # A blur wider than the offset would smear the shadow into a halo
        # instead of reading as a light, offset drop shadow.
        self.assertLess(small.shadow_blur, small.shadow_offset * 2)
        self.assertLess(large.shadow_blur, large.shadow_offset * 2)

    def test_text_shadow_is_gentler_than_the_logo_shadow(self):
        geometry = badge_geometry(1280, 720, "fetchly.example.com")
        self.assertGreaterEqual(geometry.text_shadow_offset, 1)
        self.assertLessEqual(geometry.text_shadow_offset, geometry.shadow_offset)


class ScaledSizeTests(unittest.TestCase):
    def test_caps_the_height_and_keeps_the_aspect(self):
        self.assertEqual(_scaled_size((1920, 1080), 720), (1280, 720))
        self.assertEqual(_scaled_size((1920, 1080), 480), (854, 480))

    def test_never_upscales(self):
        self.assertEqual(_scaled_size((640, 360), 720), (640, 360))

    def test_width_is_even_for_libx264(self):
        # 1080x1920 (portrait) capped at 720 gives 405 before rounding.
        width, height = _scaled_size((1080, 1920), 720)
        self.assertEqual(height, 720)
        self.assertEqual(width % 2, 0)


class VideoFilterArgsTests(unittest.TestCase):
    def _watermark(self) -> VideoWatermark:
        return VideoWatermark(path=Path("/tmp/badge.png"), width=179, height=67, margin=18)

    def test_without_a_watermark_the_plain_scale_filter_is_used(self):
        self.assertEqual(
            video_filter_args(None, scale="scale=-2:720"),
            ["-vf", "scale=-2:720"],
        )

    def test_without_a_watermark_and_without_a_scale_there_is_no_filter(self):
        self.assertEqual(video_filter_args(None), [])

    def test_scale_runs_before_the_overlay(self):
        args = video_filter_args(self._watermark(), scale="scale=-2:720")
        graph = args[args.index("-filter_complex") + 1]
        self.assertIn("[0:v]scale=-2:720[base]", graph)
        self.assertLess(graph.index("scale=-2:720"), graph.index("overlay="))

    def test_overlay_sits_in_the_bottom_right_corner(self):
        args = video_filter_args(self._watermark())
        graph = args[args.index("-filter_complex") + 1]
        self.assertIn("overlay=W-w-18:H-h-18", graph)

    def test_streams_are_mapped_explicitly_with_optional_audio(self):
        args = video_filter_args(self._watermark())
        self.assertEqual(args[-4:], ["-map", "[v]", "-map", "0:a?"])

    def test_alpha_is_flattened_before_the_encoder(self):
        args = video_filter_args(self._watermark())
        graph = args[args.index("-filter_complex") + 1]
        self.assertIn("format=yuv420p", graph)


class DrawtextEscapingTests(unittest.TestCase):
    def test_colons_in_an_ipv6_host_cannot_open_a_new_option(self):
        escaped = watermark._escape_drawtext("2001:db8::1")
        self.assertNotIn(":", escaped.replace("\\:", ""))

    def test_plain_hostname_is_unchanged(self):
        self.assertEqual(watermark._escape_drawtext("fetchly.example.com"), "fetchly.example.com")


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg is required to render the badge")
class BuildWatermarkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _probe(self, path: Path) -> tuple[int, int]:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        width, height = out.stdout.strip().split(",")[:2]
        return int(width), int(height)

    def test_badge_is_rendered_at_the_geometry_size(self):
        result = build_watermark(
            video_width=1280, video_height=720, hostname="fetchly.example.com", cache_dir=self.cache
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.path.is_file())
        self.assertEqual(self._probe(result.path), (result.width, result.height))

    def test_a_hostname_makes_the_badge_taller(self):
        plain = build_watermark(video_width=1280, video_height=720, hostname="", cache_dir=self.cache)
        titled = build_watermark(
            video_width=1280, video_height=720, hostname="fetchly.example.com", cache_dir=self.cache
        )
        assert plain is not None and titled is not None
        self.assertGreater(titled.height, plain.height)

    def test_second_call_reuses_the_cached_badge(self):
        first = build_watermark(video_width=1280, video_height=720, hostname="", cache_dir=self.cache)
        assert first is not None
        stamp = first.path.stat().st_mtime_ns

        with patch.object(watermark, "_render_badge") as render:
            second = build_watermark(video_width=1280, video_height=720, hostname="", cache_dir=self.cache)
        render.assert_not_called()
        assert second is not None
        self.assertEqual(second.path, first.path)
        self.assertEqual(second.path.stat().st_mtime_ns, stamp)

    def test_different_hostnames_get_different_cache_entries(self):
        one = build_watermark(video_width=1280, video_height=720, hostname="a.example.com", cache_dir=self.cache)
        two = build_watermark(video_width=1280, video_height=720, hostname="b.example.org", cache_dir=self.cache)
        assert one is not None and two is not None
        self.assertNotEqual(one.path, two.path)

    def test_an_ipv6_hostname_renders(self):
        result = build_watermark(
            video_width=1280, video_height=720, hostname="2001:db8::1", cache_dir=self.cache
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.path.is_file())

    def test_a_zero_sized_video_yields_no_watermark(self):
        self.assertIsNone(
            build_watermark(video_width=0, video_height=0, hostname="", cache_dir=self.cache)
        )

    def test_a_failed_render_is_not_fatal(self):
        with patch.object(watermark, "_render_badge", side_effect=OSError("boom")):
            self.assertIsNone(
                build_watermark(
                    video_width=1280, video_height=720, hostname="x.example.com", cache_dir=self.cache
                )
            )

    def test_uses_the_bundled_roboto_flex_font(self):
        self.assertTrue(watermark._FONT_TTF.is_file())
        self.assertEqual(watermark._FONT_TTF.suffix, ".ttf")
        self.assertEqual(watermark._font_file(), str(watermark._FONT_TTF))

    def test_filtergraph_blurs_the_shadow_and_references_the_bundled_font(self):
        geometry = badge_geometry(1280, 720, "fetchly.example.com")
        graph = watermark._badge_filtergraph(
            geometry, "fetchly.example.com", watermark._font_file()
        )
        self.assertIn("gblur=sigma=", graph)
        self.assertIn(str(watermark._FONT_TTF), graph)

    def test_without_a_font_the_hostname_is_dropped(self):
        with patch.object(watermark, "_font_file", return_value=""):
            result = build_watermark(
                video_width=1280, video_height=720, hostname="fetchly.example.com", cache_dir=self.cache
            )
        plain = build_watermark(video_width=1280, video_height=720, hostname="", cache_dir=self.cache)
        assert result is not None and plain is not None
        self.assertEqual(result.height, plain.height)


class WatermarkSettingTests(unittest.TestCase):
    def test_default_is_on(self):
        from app.db import _SETTINGS_DEFAULTS, _SETTINGS_TYPES

        self.assertEqual(_SETTINGS_DEFAULTS["video_watermark"], "true")
        self.assertTrue(_SETTINGS_TYPES["video_watermark"]("true"))
        self.assertFalse(_SETTINGS_TYPES["video_watermark"]("false"))

    def test_setting_reaches_the_settings_page(self):
        from app.utils.template_filters import _PUBLIC_SETTING_KEYS

        self.assertIn("video_watermark", _PUBLIC_SETTING_KEYS)


if __name__ == "__main__":
    unittest.main()
