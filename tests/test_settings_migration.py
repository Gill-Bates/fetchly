#!/usr/bin/env python3
#
# tests/test_settings_migration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Upgrades must not silently change what a running instance does.

The output setting has been renamed twice. "download_mp4_preset" became the
boolean "download_compatible_output" (a promise about the output rather than a
yt-dlp format-sort preset), which in turn became the three-way
"download_output_mode" when AV1 joined H.264/AAC as a target. Both hops carry
the stored choice over: on -> "universal", off -> "source". Without that, an
existing install would quietly start re-encoding, or quietly stop.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


class OutputModeMigrationTests(unittest.TestCase):
    def _migrate(self, key: str | None, stored: str | None) -> dict[str, str]:
        """Seed a pre-upgrade row, reopen with the current build, read settings."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                if key is not None and stored is not None:
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                            (key, stored),
                        )
                        connection.execute("DELETE FROM settings WHERE key = 'download_output_mode'")
                # Second start: the upgraded build opens the existing file.
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    rows = connection.execute("SELECT key, value FROM settings").fetchall()
        return dict(rows)

    def _assert_old_keys_gone(self, settings: dict[str, str]) -> None:
        self.assertNotIn("download_mp4_preset", settings)
        self.assertNotIn("download_compatible_output", settings)

    def test_an_enabled_promise_becomes_universal(self):
        settings = self._migrate("download_compatible_output", "true")
        self.assertEqual(settings.get("download_output_mode"), "universal")
        self._assert_old_keys_gone(settings)

    def test_a_disabled_promise_becomes_source(self):
        # Off meant "pass the source through", which is exactly "source" - not
        # the new "universal" default.
        settings = self._migrate("download_compatible_output", "false")
        self.assertEqual(settings.get("download_output_mode"), "source")
        self._assert_old_keys_gone(settings)

    def test_the_whole_chain_migrates_in_one_start(self):
        # An install that never saw the boolean still upgrades directly: the two
        # migrations run in order within the same init_db() call.
        settings = self._migrate("download_mp4_preset", "true")
        self.assertEqual(settings.get("download_output_mode"), "universal")
        self._assert_old_keys_gone(settings)

        settings = self._migrate("download_mp4_preset", "false")
        self.assertEqual(settings.get("download_output_mode"), "source")
        self._assert_old_keys_gone(settings)

    def test_an_install_without_any_old_key_is_untouched(self):
        # No row at all means the default applies at read time; the migration
        # must not invent one.
        settings = self._migrate(None, None)
        self._assert_old_keys_gone(settings)
        self.assertNotIn("download_output_mode", settings)

    def test_a_fresh_install_defaults_to_universal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(db, "DB_PATH", Path(temp_dir) / "jobs.db"):
                db.init_db()
                self.assertEqual(db.get_settings()["download_output_mode"], "universal")

    def test_the_migration_runs_only_once(self):
        # A later manual change must not be overwritten by a stale old row on
        # the next start: the old keys are gone, so there is nothing to re-copy.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO settings (key, value) "
                        "VALUES ('download_compatible_output', 'true')"
                    )
                db.init_db()
                db.set_settings({"download_output_mode": "av1"})
                db.init_db()
                self.assertEqual(db.get_settings()["download_output_mode"], "av1")

    def test_the_setting_reaches_the_settings_page(self):
        from app.utils.template_filters import _PUBLIC_SETTING_KEYS

        self.assertIn("download_output_mode", _PUBLIC_SETTING_KEYS)
        self.assertNotIn("download_compatible_output", _PUBLIC_SETTING_KEYS)
        self.assertNotIn("download_mp4_preset", _PUBLIC_SETTING_KEYS)


if __name__ == "__main__":
    unittest.main()
