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

    def test_bulk_removal_deletes_selected_jobs_and_invalidates_their_links(self) -> None:
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
                self.assertEqual(db.delete_jobs_and_share_links(job_ids), (2, 2))
                self.assertEqual(db.list_job_ids(), [])

                with sqlite3.connect(db_path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM share_links").fetchone()[0], 0)

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


if __name__ == "__main__":
    unittest.main()
