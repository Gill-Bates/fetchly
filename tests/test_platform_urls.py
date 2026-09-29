#!/usr/bin/env python3
#
# tests/test_platform_urls.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest

from app.utils.platform import PLATFORM_FACEBOOK, PLATFORM_YOUTUBE, detect_platform, validate_media_url


class FacebookStoryUrlTests(unittest.TestCase):
    def test_accepts_shared_story_permalink(self) -> None:
        url = (
            "https://www.facebook.com/stories/122108714109304856/"
            "UzpfSVNDOjQ3MzkzMzkwNDk3MjQ0NDk=/?view_single=1&source=shared_permalink"
        )
        self.assertEqual(detect_platform(url), PLATFORM_FACEBOOK)
        self.assertEqual(validate_media_url(url), (True, ""))

    def test_accepts_story_set_and_percent_encoded_padding(self) -> None:
        for url in (
            "https://www.facebook.com/stories/122108714109304856",
            "https://www.facebook.com/stories/122108714109304856/UzpfSVNDOjQ3MzkzMzkwNDk3MjQ0NDk%3D/",
            "https://m.facebook.com/stories/122108714109304856/UzpfSVNDOjQ3MzkzMzkwNDk3MjQ0NDk==",
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_media_url(url), (True, ""))

    def test_rejects_story_without_numeric_set_id(self) -> None:
        for url in (
            "https://www.facebook.com/stories",
            "https://www.facebook.com/stories/not-a-set-id",
            "https://www.facebook.com/stories/122108714109304856/bad token",
        ):
            with self.subTest(url=url):
                is_valid, error = validate_media_url(url)
                self.assertFalse(is_valid)
                self.assertIn("Invalid Facebook URL", error)


class YouTubeShortsUrlTests(unittest.TestCase):
    def test_accepts_shorts_url_with_and_without_query(self) -> None:
        for url in (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share",
            "https://m.youtube.com/shorts/dQw4w9WgXcQ",
        ):
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), PLATFORM_YOUTUBE)
                self.assertEqual(validate_media_url(url), (True, ""))

    def test_rejects_shorts_url_without_a_video_id(self) -> None:
        for url in (
            "https://www.youtube.com/shorts",
            "https://www.youtube.com/shorts/tooshort",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ/extra",
        ):
            with self.subTest(url=url):
                is_valid, error = validate_media_url(url)
                self.assertFalse(is_valid)
                self.assertIn("Invalid YouTube URL", error)
