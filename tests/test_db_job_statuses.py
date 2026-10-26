#!/usr/bin/env python3
#
# tests/test_db_job_statuses.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from tests._support import IsolatedDbTestCase


class JobStatusSchemaTests(unittest.TestCase):
    def test_schema_accepts_every_application_status_and_rejects_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    for index, status in enumerate(sorted(db._JOB_STATUSES)):
                        connection.execute(
                            "INSERT INTO jobs (id, url, status) VALUES (?, ?, ?)",
                            (f"job-{index}", "https://example.com/video", status),
                        )

                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO jobs (id, url, status) VALUES (?, ?, ?)",
                            ("invalid-job", "https://example.com/video", "unknown"),
                        )

    def test_bulk_removal_deletes_selected_jobs_and_invalidates_their_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                job_ids = ["job-one", "job-two"]
                for job_id in job_ids:
                    db.insert_job(job_id, "https://example.com/video", "video", "max", "queued")

                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                        ("share-one", job_ids[0], 0),
                    )
                    connection.execute(
                        "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                        ("share-two", job_ids[1], 3),
                    )
                    connection.commit()

                self.assertCountEqual(db.list_job_ids(), job_ids)
                deleted_ids, links_deleted = db.delete_jobs_and_share_links(job_ids, terminal_only=False)
                self.assertCountEqual(deleted_ids, job_ids)
                self.assertEqual(links_deleted, 2)
                self.assertEqual(db.list_job_ids(), [])

                with sqlite3.connect(db_path) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM share_links").fetchone()[0],
                        0,
                    )

    def test_conditional_delete_keeps_jobs_that_became_active(self) -> None:
        """A job retried between selection and delete keeps its row and share link."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                for job_id, status in (("finished", "done"), ("retried", "error")):
                    db.insert_job(job_id, "https://example.com/video", "video", "max", status)
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            "INSERT INTO share_links (token, job_id) VALUES (?, ?)",
                            (f"share-{job_id}", job_id),
                        )
                selected = ["finished", "retried"]
                db.update_job("retried", status="queued")

                deleted_ids, links_deleted = db.delete_jobs_and_share_links(selected)

                self.assertEqual(deleted_ids, ["finished"])
                self.assertEqual(links_deleted, 1)
                self.assertEqual(db.list_job_ids(), ["retried"])
                with sqlite3.connect(db_path) as connection:
                    tokens = [r[0] for r in connection.execute("SELECT token FROM share_links")]
                self.assertEqual(tokens, ["share-retried"])

    def test_zero_retention_is_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        """
                        INSERT INTO jobs (id, url, type, status, finished_at)
                        VALUES (?, ?, ?, ?, datetime('now', '-30 days'))
                        """,
                        ("old-job", "https://example.com/video", "video", "done"),
                    )
                    connection.commit()

                self.assertEqual(db.get_settings()["retention_days"], 0)
                self.assertEqual(db.list_expired_job_ids(0), [])


class JobUpdateGuardTests(IsolatedDbTestCase):
    """update_job and update_job_if_status share one field and status allow-list."""

    def setUp(self) -> None:
        super().setUp()
        db.insert_job("job-1", "https://example.com/video", "video", "max", "queued")

    def _updaters(self):
        return (
            ("update_job", lambda **fields: db.update_job("job-1", **fields)),
            (
                "update_job_if_status",
                lambda **fields: db.update_job_if_status("job-1", ("queued",), **fields),
            ),
        )

    def test_unknown_and_invalid_fields_are_rejected(self) -> None:
        for name, update in self._updaters():
            with self.subTest(updater=name, case="unknown field"):
                with self.assertRaises(ValueError):
                    update(not_a_column="x")
            with self.subTest(updater=name, case="sql in column name"):
                with self.assertRaises(ValueError):
                    update(**{"status=status, url": "x"})
            with self.subTest(updater=name, case="unknown status"):
                with self.assertRaises(ValueError):
                    update(status="finished")
        self.assertEqual(db.get_job("job-1")["status"], "queued")

    def test_empty_updates_change_nothing(self) -> None:
        for name, update in self._updaters():
            with self.subTest(updater=name):
                self.assertFalse(update())

    def test_valid_updates_are_applied(self) -> None:
        self.assertTrue(db.update_job("job-1", status="downloading", message="working"))
        self.assertFalse(db.update_job_if_status("job-1", ("queued",), status="done"))
        self.assertTrue(db.update_job_if_status("job-1", ("downloading",), status="done"))
        self.assertEqual(db.get_job("job-1")["status"], "done")

    def test_if_status_requires_expected_statuses(self) -> None:
        with self.assertRaises(ValueError):
            db.update_job_if_status("job-1", (), status="done")


if __name__ == "__main__":
    unittest.main()
