#!/usr/bin/env python3
#
# tests/test_cookie_files.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils import cookies


class CookieFileLookupTests(unittest.TestCase):
    def test_custom_project_and_data_directories_follow_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            custom_dir = root / "custom"
            project_dir = root / "project"
            data_dir = root / "data"
            for directory in (custom_dir, project_dir, data_dir):
                directory.mkdir()
                (directory / "youtube_cookies.txt").write_text(
                    directory.name,
                    encoding="utf-8",
                )

            with (
                patch.object(cookies, "_PROJECT_COOKIES_DIR", project_dir),
                patch.object(cookies, "_DATA_COOKIES_DIR", data_dir),
                patch.dict(os.environ, {"FETCHLY_COOKIES_DIR": str(custom_dir)}),
            ):
                self.assertEqual(
                    cookies.find_cookie_file("youtube_cookies.txt"),
                    custom_dir / "youtube_cookies.txt",
                )

                (custom_dir / "youtube_cookies.txt").unlink()
                self.assertEqual(
                    cookies.find_cookie_file("youtube_cookies.txt"),
                    project_dir / "youtube_cookies.txt",
                )

                (project_dir / "youtube_cookies.txt").unlink()
                self.assertEqual(
                    cookies.find_cookie_file("youtube_cookies.txt"),
                    data_dir / "youtube_cookies.txt",
                )

    def test_missing_file_has_separate_optional_and_fallback_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            data_dir = Path(temp_dir) / "data"
            project_dir.mkdir()
            data_dir.mkdir()

            with (
                patch.object(cookies, "_PROJECT_COOKIES_DIR", project_dir),
                patch.object(cookies, "_DATA_COOKIES_DIR", data_dir),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertIsNone(cookies.find_cookie_file("youtube_cookies.txt"))
                self.assertEqual(
                    cookies.default_cookie_file("youtube_cookies.txt"),
                    project_dir / "youtube_cookies.txt",
                )


if __name__ == "__main__":
    unittest.main()
