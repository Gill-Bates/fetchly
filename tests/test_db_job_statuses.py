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


if __name__ == "__main__":
    unittest.main()
