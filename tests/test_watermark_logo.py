#!/usr/bin/env python3
#
# tests/test_watermark_logo.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The custom watermark logo: what is accepted, and what the badge does with it.

What the server sees is always a PNG - a dropped PNG as the user exported it,
or the browser's render of a dropped SVG - and these tests are the boundary
that matters: every property is re-derived from the bytes, never taken from
what the client said about them. A user's own PNG is why the odd encodings
below (palette, 16-bit) are covered: a canvas render only ever produced rgba.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils import watermark, watermark_logo
from app.utils.watermark import build_watermark
from app.utils.watermark_logo import (
    LogoRejectedError,
    LogoStatus,
    logo_status,
    remove_custom_logo,
    store_custom_logo,
    validate_logo_bytes,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _png(source: str, target: Path, *, filters: str = "", pix_fmt: str = "rgba") -> bytes:
    """Render a test PNG with ffmpeg and return its bytes."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source]
    if filters:
        cmd.extend(["-vf", filters])
    cmd.extend(["-frames:v", "1", "-pix_fmt", pix_fmt, str(target)])
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return target.read_bytes()


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg/ffprobe not available")
class LogoValidationTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.target = self.dir / "watermark-logo.png"
        self.addCleanup(self._temp.cleanup)

    def _usable_logo(self) -> bytes:
        # Transparent canvas with an opaque mark on it - what a real logo is.
        return _png(
            "color=c=black@0.0:s=400x120,format=rgba",
            self.dir / "src.png",
            filters="drawbox=x=20:y=20:w=200:h=60:color=white@1.0:t=fill,format=rgba",
        )

    def test_a_transparent_logo_is_accepted_and_measured(self):
        status = validate_logo_bytes(self._usable_logo(), self.target)
        self.assertTrue(status.custom)
        self.assertEqual((status.width, status.height), (400, 120))
        self.assertTrue(status.fingerprint)
        self.assertTrue(self.target.is_file())

    def test_an_empty_file_is_rejected(self):
        with self.assertRaises(LogoRejectedError):
            validate_logo_bytes(b"", self.target)

    def test_an_oversized_file_is_rejected_before_it_is_written(self):
        with self.assertRaises(LogoRejectedError):
            validate_logo_bytes(b"x" * (watermark_logo.MAX_LOGO_BYTES + 1), self.target)
        self.assertFalse(self.target.exists())

    def test_a_file_that_is_not_an_image_is_rejected(self):
        # ffprobe guesses "png" from the name and exits 0 with a zero size, so
        # the size check is what actually catches this.
        with self.assertRaises(LogoRejectedError):
            validate_logo_bytes(b"not an image at all", self.target)

    def test_a_fully_opaque_image_is_rejected(self):
        data = _png("color=c=red:s=400x120,format=rgba", self.dir / "opaque.png")
        with self.assertRaisesRegex(LogoRejectedError, "opaque"):
            validate_logo_bytes(data, self.target)

    def test_a_16_bit_transparent_logo_is_accepted(self):
        # rgba64's alpha runs 0-65535. Read without normalizing, a transparent
        # image looks opaque, and every 16-bit logo would be refused.
        data = _png(
            "color=c=black@0.0:s=400x120,format=rgba",
            self.dir / "deep.png",
            filters="drawbox=x=20:y=20:w=200:h=60:color=white@1.0:t=fill,format=rgba",
            pix_fmt="rgba64be",
        )
        status = validate_logo_bytes(data, self.target)
        self.assertEqual((status.width, status.height), (400, 120))

    def test_an_opaque_palette_image_is_rejected(self):
        # pal8 is accepted as a pixel format because a palette can carry
        # transparency; this one does not, and only the alpha probe can tell.
        data = _png("color=c=red:s=400x120,format=rgba", self.dir / "pal.png", pix_fmt="pal8")
        with self.assertRaisesRegex(LogoRejectedError, "opaque"):
            validate_logo_bytes(data, self.target)

    def test_a_too_small_image_is_rejected(self):
        data = _png("color=c=black@0.0:s=16x16,format=rgba", self.dir / "tiny.png")
        with self.assertRaisesRegex(LogoRejectedError, "smaller"):
            validate_logo_bytes(data, self.target)

    def test_an_elongated_image_is_rejected(self):
        # 4000x40 is 100:1 - a sliver in a video corner.
        data = _png("color=c=black@0.0:s=4000x40,format=rgba", self.dir / "thin.png")
        with self.assertRaisesRegex(LogoRejectedError, "elongated"):
            validate_logo_bytes(data, self.target)

    def test_a_rejected_upload_leaves_no_temporary_file_behind(self):
        with self.assertRaises(LogoRejectedError):
            validate_logo_bytes(b"not an image at all", self.target)
        self.assertEqual(list(self.dir.glob("*.tmp.png")), [])

    def test_a_rejected_upload_does_not_replace_an_installed_logo(self):
        good = self._usable_logo()
        validate_logo_bytes(good, self.target)
        with self.assertRaises(LogoRejectedError):
            validate_logo_bytes(b"junk", self.target)
        self.assertEqual(self.target.read_bytes(), good)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg/ffprobe not available")
class LogoLifecycleTests(unittest.TestCase):
    """store -> status -> remove, against a data directory of its own."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        patcher = patch.object(watermark_logo, "get_data_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logo = _png(
            "color=c=black@0.0:s=400x120,format=rgba",
            self.dir / "src.png",
            filters="drawbox=x=20:y=20:w=200:h=60:color=white@1.0:t=fill,format=rgba",
        )

    def test_the_logo_lives_in_its_own_directory_on_the_data_volume(self):
        # Next to "cookies" and "watermark-cache": one named place per kind of
        # file, so the volume stays legible to whoever mounts or backs it up.
        store_custom_logo(self.logo)
        self.assertEqual(watermark_logo.custom_logo_path(), self.dir / "logo" / "watermark-logo.png")
        self.assertTrue((self.dir / "logo" / "watermark-logo.png").is_file())

    def test_no_logo_reports_the_bundled_artwork(self):
        self.assertEqual(logo_status(), LogoStatus(custom=False))

    def test_a_stored_logo_is_reported_with_its_size(self):
        store_custom_logo(self.logo)
        status = logo_status()
        self.assertTrue(status.custom)
        self.assertEqual((status.width, status.height), (400, 120))

    def test_removing_reports_whether_there_was_anything_to_remove(self):
        self.assertFalse(remove_custom_logo())
        store_custom_logo(self.logo)
        self.assertTrue(remove_custom_logo())
        self.assertFalse(logo_status().custom)

    def test_an_unreadable_stored_logo_reads_as_absent(self):
        # Truncated on disk: the UI must offer a fresh upload, not a broken
        # preview, and the badge must fall back to the bundled artwork.
        store_custom_logo(self.logo)
        watermark_logo.custom_logo_path().write_bytes(b"truncated")
        self.assertFalse(logo_status().custom)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg/ffprobe not available")
class BadgeUsesTheCustomLogoTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.cache = self.dir / "cache"
        self.addCleanup(self._temp.cleanup)
        for module in (watermark_logo, watermark):
            patcher = patch.object(module, "get_data_dir", return_value=self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logo = _png(
            "color=c=black@0.0:s=400x120,format=rgba",
            self.dir / "src.png",
            filters="drawbox=x=20:y=20:w=200:h=60:color=white@1.0:t=fill,format=rgba",
        )

    def _badge(self):
        return build_watermark(
            video_width=1920, video_height=1080, hostname="example.com", cache_dir=self.cache
        )

    def test_installing_a_logo_produces_a_different_badge(self):
        bundled = self._badge()
        assert bundled is not None
        store_custom_logo(self.logo)
        custom = self._badge()
        assert custom is not None
        # A different cache entry, so a replaced logo can never be served from
        # a badge rendered with the previous one.
        self.assertNotEqual(bundled.path, custom.path)
        # 400x120 is a different aspect than the bundled artwork, and the badge
        # follows it instead of squeezing the logo.
        self.assertNotEqual(bundled.height, custom.height)

    def test_removing_the_logo_restores_the_bundled_badge(self):
        bundled = self._badge()
        store_custom_logo(self.logo)
        self._badge()
        remove_custom_logo()
        assert bundled is not None
        self.assertEqual(self._badge().path, bundled.path)

    def test_the_badge_keeps_the_custom_logos_proportions(self):
        store_custom_logo(self.logo)
        geometry = watermark.badge_geometry(1920, 1080, "", 400 / 120)
        self.assertAlmostEqual(geometry.logo_width / geometry.logo_height, 400 / 120, places=1)


if __name__ == "__main__":
    unittest.main()
