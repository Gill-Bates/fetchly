#!/usr/bin/env python3
#
# tests/test_media_url_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authoritative media-URL acceptance corpus.

``app/utils/platform.py`` is the single authority for which media URLs are
accepted. This corpus pins that decision across the YouTube, TikTok, Instagram
and Facebook grammars plus the cross-cutting rejections (scheme, credentials,
custom port, copy/paste damage). The frontend preflight in
``app/static/js/utils.js`` deliberately does not reproduce this grammar; this
corpus is what guards the server contract the preflight defers to, so it must
not change when the frontend is reduced to a cheap host/scheme check.
"""

import unittest

from app.utils.platform import (
    PLATFORM_FACEBOOK,
    PLATFORM_INSTAGRAM,
    PLATFORM_TIKTOK,
    PLATFORM_YOUTUBE,
    detect_platform,
    validate_media_url,
)

# Known-valid URLs, grouped so a failure names the grammar at fault.
VALID_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    PLATFORM_YOUTUBE: (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL0000000000",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/v/dQw4w9WgXcQ",
    ),
    PLATFORM_TIKTOK: (
        "https://www.tiktok.com/@someuser/video/1234567890123456789",
        "https://www.tiktok.com/@some.user_1/photo/1234567890123456789",
        "https://vm.tiktok.com/ZMabc123/",
        "https://vt.tiktok.com/ZMabc123/",
        "https://www.tiktok.com/t/ZMabc123/",
    ),
    PLATFORM_INSTAGRAM: (
        "https://www.instagram.com/p/Cabcdef1234/",
        "https://www.instagram.com/reel/Cabcdef1234/",
        "https://www.instagram.com/tv/Cabcdef1234/",
    ),
    PLATFORM_FACEBOOK: (
        "https://www.facebook.com/watch/?v=1234567890",
        "https://www.facebook.com/watch?v=1234567890",
        "https://www.facebook.com/video.php?v=1234567890",
        "https://www.facebook.com/reel/1234567890",
        "https://www.facebook.com/stories/122108714109304856",
        (
            "https://www.facebook.com/stories/122108714109304856/"
            "UzpfSVNDOjQ3MzkzMzkwNDk3MjQ0NDk=/?view_single=1&source=shared_permalink"
        ),
        "https://www.facebook.com/share/v/abcDEF123/",
        "https://www.facebook.com/share/r/abcDEF123/",
        "https://fb.watch/abcDEF123/",
        "https://www.facebook.com/somepage/videos/1234567890",
        "https://www.facebook.com/somepage/videos/some-slug/1234567890",
    ),
}

# Known-invalid URLs paired with a fragment of the authoritative error message.
INVALID_WITH_MESSAGE: tuple[tuple[str, str], ...] = (
    # Cross-cutting rejections (scheme / credentials / port / shape).
    ("", "URL is required"),
    ("http://www.youtube.com/watch?v=dQw4w9WgXcQ", "must start with https://"),
    ("https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ", "must not contain credentials"),
    ("https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ", "must not use a custom port"),
    ("https://example.com/watch?v=dQw4w9WgXcQ", "Unsupported URL"),
    # Per-platform grammar rejections.
    ("https://www.youtube.com/watch?v=tooshort", "Invalid YouTube URL"),
    ("https://www.youtube.com/shorts", "Invalid YouTube URL"),
    ("https://www.tiktok.com/@someuser/video/abc", "Invalid TikTok URL"),
    ("https://www.tiktok.com/@someuser", "Invalid TikTok URL"),
    ("https://www.instagram.com/p/", "Invalid Instagram URL"),
    ("https://www.instagram.com/someuser", "Invalid Instagram URL"),
    ("https://www.facebook.com/watch?v=abc", "Invalid Facebook URL"),
    ("https://www.facebook.com/stories/not-a-set-id", "Invalid Facebook URL"),
)


class MediaUrlAcceptanceCorpusTests(unittest.TestCase):
    """The authoritative accept/reject decision for every supported grammar."""

    def test_accepts_every_valid_url(self) -> None:
        for platform, urls in VALID_BY_PLATFORM.items():
            for url in urls:
                with self.subTest(platform=platform, url=url):
                    self.assertEqual(detect_platform(url), platform)
                    self.assertEqual(validate_media_url(url), (True, ""))

    def test_rejects_every_invalid_url_with_its_message(self) -> None:
        for url, expected_fragment in INVALID_WITH_MESSAGE:
            with self.subTest(url=url):
                is_valid, error = validate_media_url(url)
                self.assertFalse(is_valid)
                self.assertIn(expected_fragment, error)


class MediaUrlCopyPasteNormalizationTests(unittest.TestCase):
    """Copy/paste damage (surrounding whitespace, HTML-escaped ``&``,
    zero-width characters) must not change the accept decision.
    """

    def test_accepts_url_with_surrounding_whitespace(self) -> None:
        self.assertEqual(
            validate_media_url("  https://www.youtube.com/watch?v=dQw4w9WgXcQ  "),
            (True, ""),
        )

    def test_accepts_url_with_html_escaped_ampersand(self) -> None:
        self.assertEqual(
            validate_media_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;list=PL0000000000"),
            (True, ""),
        )

    def test_accepts_url_with_zero_width_characters(self) -> None:
        self.assertEqual(
            validate_media_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ\u200b"),
            (True, ""),
        )


if __name__ == "__main__":
    unittest.main()
