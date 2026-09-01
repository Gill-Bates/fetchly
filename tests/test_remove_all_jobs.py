#!/usr/bin/env python3
#
# tests/test_remove_all_jobs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-remove-jobs-secret")

from app import db
from app.routes import api


class RemoveAllJobsTests(unittest.TestCase):
    def test_removes_job_rows_artifacts_and_share_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            db_path = data_dir / "jobs.db"
            job_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

            with patch.object(db, "DB_PATH", db_path), patch.object(api, "get_data_dir", return_value=data_dir):
                db.init_db()
                for job_id in job_ids:
                    db.insert_job(job_id, "https://example.com/video", "video", "max", "queued")
                    job_dir = data_dir / job_id
                    job_dir.mkdir()
                    (job_dir / "download.mp4").write_bytes(b"video")

                with db.get_db() as connection:
                    connection.execute(
                        "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                        ("share-link-one", job_ids[0], 0),
                    )
                    connection.execute(
                        "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                        ("share-link-two", job_ids[1], 1),
                    )
                    connection.commit()

                request = Request({"type": "http", "method": "POST", "path": "/api/jobs/remove-all"})
                result = asyncio.run(api.api_remove_all_jobs(request, "test-user"))

                self.assertEqual(result["jobs_deleted"], 2)
                self.assertEqual(result["files_deleted"], 2)
                self.assertEqual(result["share_links_deleted"], 2)
                self.assertEqual(db.list_job_ids(), [])
                self.assertTrue(all(not (data_dir / job_id).exists() for job_id in job_ids))
                with db.get_db() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM share_links").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
