#!/usr/bin/env python3
#
# tests/test_cookie_files.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils import cookies


class CookieFileLookupTests(unittest.TestCase):
    def test_find_cookie_file_resolves_against_the_data_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "cookies"
            data_dir.mkdir()

            with patch.object(cookies, "_DATA_COOKIES_DIR", data_dir):
                self.assertIsNone(cookies.find_cookie_file("youtube_cookies.txt"))

                (data_dir / "youtube_cookies.txt").write_text("x", encoding="utf-8")
                self.assertEqual(
                    cookies.find_cookie_file("youtube_cookies.txt"),
                    data_dir / "youtube_cookies.txt",
                )

    def test_missing_file_has_separate_optional_and_fallback_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "cookies"
            data_dir.mkdir()

            with patch.object(cookies, "_DATA_COOKIES_DIR", data_dir):
                self.assertIsNone(cookies.find_cookie_file("youtube_cookies.txt"))
                self.assertEqual(
                    cookies.default_cookie_file("youtube_cookies.txt"),
                    data_dir / "youtube_cookies.txt",
                )

    def test_empty_filename_never_resolves(self) -> None:
        self.assertIsNone(cookies.find_cookie_file(""))

    def test_ensure_data_cookies_dir_creates_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_cookies_dir = Path(temp_dir) / "data" / "cookies"
            self.assertFalse(data_cookies_dir.exists())

            with patch.object(cookies, "_DATA_COOKIES_DIR", data_cookies_dir):
                cookies.ensure_data_cookies_dir()
                self.assertTrue(data_cookies_dir.is_dir())

                # Idempotent: calling again on an already-existing directory
                # must not raise.
                cookies.ensure_data_cookies_dir()
                self.assertTrue(data_cookies_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
