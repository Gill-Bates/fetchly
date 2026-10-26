#!/usr/bin/env python3
#
# tests/test_db_schema.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A fresh database carries the complete schema, and re-initialising it is a no-op.

There is no migration path: ``init_db()`` only creates what is missing and rejects
an existing database whose tables lack expected columns with ``DatabaseSchemaError``.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


def _schema_dump(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()


class FreshSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "jobs.db"
        patcher = patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        db._database_path_prepared = None
        self.addCleanup(setattr, db, "_database_path_prepared", None)
        self.addCleanup(db.close_db)

    def test_fresh_database_has_all_tables_columns_and_indexes(self) -> None:
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            job_columns = {r[1] for r in connection.execute("PRAGMA table_info(jobs)")}
            indexes = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"jobs", "audio_analysis_cache", "share_links", "settings"} <= tables)
        self.assertTrue({"include_in_history", "client_ip", "lalal_split_done", "bpm_confidence"} <= job_columns)
        self.assertTrue(
            {
                "idx_jobs_created_at_id",
                "idx_jobs_history_created_at",
                "idx_jobs_status_finished_at",
                "idx_jobs_status_created_at",
                "idx_jobs_audio_analysis",
                "idx_jobs_url_type_quality",
                "idx_share_links_job_id",
            }
            <= indexes
        )

    def test_initialising_twice_is_idempotent(self) -> None:
        db.init_db()
        first = _schema_dump(self.db_path)
        db.job_exists("missing")
        db.init_db()
        self.assertEqual(first, _schema_dump(self.db_path))

    def test_init_does_not_stamp_a_schema_version(self) -> None:
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_output_mode_defaults_to_universal(self) -> None:
        db.init_db()
        self.assertEqual(db.get_settings()["download_output_mode"], "universal")

    def test_the_setting_reaches_the_settings_page(self) -> None:
        from app.utils.template_filters import _PUBLIC_SETTING_KEYS

        self.assertIn("download_output_mode", _PUBLIC_SETTING_KEYS)


class ExistingSchemaVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "jobs.db"
        patcher = patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        db._database_path_prepared = None
        self.addCleanup(setattr, db, "_database_path_prepared", None)
        self.addCleanup(db.close_db)

    def _make_legacy_db(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL)")
            connection.execute("INSERT INTO jobs (id, url, status) VALUES ('a', 'u', 'done')")

    def test_legacy_jobs_table_is_rejected_with_actionable_message(self) -> None:
        self._make_legacy_db()
        with self.assertRaises(db.DatabaseSchemaError) as ctx:
            db.init_db()
        message = str(ctx.exception)
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertIn("jobs.type", message)
        self.assertIn("jobs.include_in_history", message)
        self.assertIn("... and ", message)
        self.assertIn(str(self.db_path.resolve()), message)
        self.assertIn("no longer migrated", message)
        self.assertIn("jobs.db-wal", message)
        self.assertIn("jobs and settings are lost", message)

    def test_rejected_database_is_left_untouched(self) -> None:
        self._make_legacy_db()
        before = _schema_dump(self.db_path)
        with self.assertRaises(db.DatabaseSchemaError):
            db.init_db()
        self.assertTrue(self.db_path.exists())
        self.assertEqual(before, _schema_dump(self.db_path))
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT id FROM jobs").fetchall(), [("a",)])

    def test_missing_column_in_a_secondary_table_names_that_table(self) -> None:
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DROP TABLE settings")
            connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY)")
        with self.assertRaises(db.DatabaseSchemaError) as ctx:
            db.init_db()
        self.assertIn("settings.value", str(ctx.exception))

    def test_long_missing_column_list_is_truncated(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
        with self.assertRaises(db.DatabaseSchemaError) as ctx:
            db.init_db()
        message = str(ctx.exception)
        self.assertIn("jobs.url", message)
        self.assertRegex(message, r"\.\.\. and \d+ more")
        self.assertNotIn("jobs.client_ip", message)  # beyond the listed cut-off

    def test_file_that_is_not_a_database_is_rejected_and_left_untouched(self) -> None:
        payload = bytes(range(256)) * 16
        self.db_path.write_bytes(payload)
        with self.assertRaises(db.DatabaseSchemaError) as ctx:
            db.init_db()
        message = str(ctx.exception)
        self.assertIn(str(self.db_path.resolve()), message)
        self.assertIn("jobs.db-wal", message)
        self.assertIn("back them up", message)
        self.assertEqual(self.db_path.read_bytes(), payload)

    def test_locked_database_is_not_reported_as_a_schema_problem(self) -> None:
        exc = sqlite3.OperationalError("database is locked")
        self.assertFalse(db._is_unreadable_database(exc))

    def test_missing_table_is_created_not_rejected(self) -> None:
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DROP TABLE audio_analysis_cache")
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            columns = {r[1] for r in connection.execute("PRAGMA table_info(audio_analysis_cache)")}
        self.assertEqual(columns, {"hash", "bpm", "bpm_confidence", "created_at"})

    def test_valid_database_survives_repeated_initialisation(self) -> None:
        db.init_db()
        db.init_db()
        db.init_db()

    def test_extra_columns_and_tables_are_tolerated(self) -> None:
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("ALTER TABLE jobs ADD COLUMN foreign_col TEXT")
            connection.execute("CREATE TABLE foreign_table (x INTEGER)")
        db.init_db()
        with sqlite3.connect(self.db_path) as connection:
            columns = {r[1] for r in connection.execute("PRAGMA table_info(jobs)")}
        self.assertIn("foreign_col", columns)


if __name__ == "__main__":
    unittest.main()
