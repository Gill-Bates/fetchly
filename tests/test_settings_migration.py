#!/usr/bin/env python3
#
# tests/test_settings_migration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Upgrades must not silently change what a running instance does.

"download_mp4_preset" became "download_compatible_output" (same meaning, a
promise about the output rather than a yt-dlp format-sort preset), and the
default flipped from on to off. Without carrying the stored value over, every
existing install would quietly start producing .webm files.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


class CompatibleOutputMigrationTests(unittest.TestCase):
    def _migrate(self, stored: str | None) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                if stored is not None:
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO settings (key, value) VALUES ('download_mp4_preset', ?)",
                            (stored,),
                        )
                        connection.execute("DELETE FROM settings WHERE key = 'download_compatible_output'")
                # Second start: the upgraded build opens the existing file.
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    rows = connection.execute("SELECT key, value FROM settings").fetchall()
        return dict(rows)

    def test_an_enabled_preset_becomes_the_compatibility_promise(self):
        settings = self._migrate("true")
        self.assertEqual(settings.get("download_compatible_output"), "true")
        self.assertNotIn("download_mp4_preset", settings)

    def test_a_disabled_preset_carries_over_too(self):
        settings = self._migrate("false")
        self.assertEqual(settings.get("download_compatible_output"), "false")
        self.assertNotIn("download_mp4_preset", settings)

    def test_an_install_without_the_old_key_is_untouched(self):
        settings = self._migrate(None)
        self.assertNotIn("download_mp4_preset", settings)
        self.assertNotIn("download_compatible_output", settings)

    def test_the_migration_runs_only_once(self):
        # A later manual change must not be overwritten by a stale old row on
        # the next start: the old key is gone, so there is nothing to re-copy.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES ('download_mp4_preset', 'true')"
                    )
                db.init_db()
                db.set_settings({"download_compatible_output": False})
                db.init_db()
                self.assertFalse(db.get_settings()["download_compatible_output"])

    def test_the_setting_reaches_the_settings_page(self):
        from app.utils.template_filters import _PUBLIC_SETTING_KEYS

        self.assertIn("download_compatible_output", _PUBLIC_SETTING_KEYS)
        self.assertNotIn("download_mp4_preset", _PUBLIC_SETTING_KEYS)


if __name__ == "__main__":
    unittest.main()
