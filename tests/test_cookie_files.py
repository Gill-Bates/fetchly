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
                # Owner-only: the directory listing alone reveals which
                # platforms hold a live session.
                self.assertEqual(data_cookies_dir.stat().st_mode & 0o777, 0o700)

                # Idempotent: calling again on an already-existing directory
                # must not raise.
                cookies.ensure_data_cookies_dir()
                self.assertTrue(data_cookies_dir.is_dir())

    def test_ensure_data_cookies_dir_repairs_a_permissive_directory(self) -> None:
        """mkdir's mode only applies on creation - an existing directory left
        world-readable by an earlier release must be tightened as well."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_cookies_dir = Path(temp_dir) / "data" / "cookies"
            data_cookies_dir.mkdir(parents=True)
            data_cookies_dir.chmod(0o775)

            with patch.object(cookies, "_DATA_COOKIES_DIR", data_cookies_dir):
                cookies.ensure_data_cookies_dir()

            self.assertEqual(data_cookies_dir.stat().st_mode & 0o777, 0o700)

    def test_ensure_data_cookies_dir_survives_an_unwritable_permission_bit(self) -> None:
        """A data volume owned by another account must not fail startup."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_cookies_dir = Path(temp_dir) / "data" / "cookies"
            data_cookies_dir.mkdir(parents=True)

            with (
                patch.object(cookies, "_DATA_COOKIES_DIR", data_cookies_dir),
                patch.object(Path, "chmod", side_effect=PermissionError("not owner")),
            ):
                cookies.ensure_data_cookies_dir()

            self.assertTrue(data_cookies_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
