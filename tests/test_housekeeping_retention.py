#!/usr/bin/env python3
#
# tests/test_housekeeping_retention.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Retention must clean the database row, the job directory, and share links
together - not just the filesystem artifacts. See app/main.py's
_run_housekeeping_once() and app/db.py's purge_old_jobs()."""

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

from app import db
from tests._support import IsolatedDbTestCase


def _insert_finished_job(db_path, job_id: str, *, days_old: int, status: str = "done") -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO jobs (id, url, type, status, finished_at)
            VALUES (?, ?, ?, ?, datetime('now', '-' || ? || ' days'))
            """,
            (job_id, "https://example.com/video", "video", status, days_old),
        )
        connection.commit()


class PurgeOldJobsTests(IsolatedDbTestCase):
    def test_purges_expired_terminal_jobs_and_returns_their_ids(self) -> None:
        old_id = str(uuid.uuid4())
        recent_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, old_id, days_old=30)
        _insert_finished_job(db.DB_PATH, recent_id, days_old=1)

        deleted = db.purge_old_jobs(7)

        self.assertEqual(deleted, [old_id])
        self.assertCountEqual(db.list_job_ids(), [recent_id])

    def test_zero_keep_days_purges_everything_terminal(self) -> None:
        # purge_old_jobs() itself has no "0 == unlimited" special case - that
        # guard lives in main.py's _run_housekeeping_once(), which must never
        # call this with keep_days == 0. finished_at must be strictly older
        # than "now - 0 days", so back-date it by a full day to avoid a
        # sub-second race against datetime('now').
        job_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, job_id, days_old=1)

        deleted = db.purge_old_jobs(0)

        self.assertEqual(deleted, [job_id])
        self.assertEqual(db.list_job_ids(), [])

    def test_non_terminal_jobs_are_never_purged(self) -> None:
        queued_id = str(uuid.uuid4())
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO jobs (id, url, type, status) VALUES (?, ?, ?, ?)",
                (queued_id, "https://example.com/video", "video", "queued"),
            )
            connection.commit()

        deleted = db.purge_old_jobs(0)

        self.assertEqual(deleted, [])
        self.assertCountEqual(db.list_job_ids(), [queued_id])


class HousekeepingSweepIntegrationTests(IsolatedDbTestCase):
    """The retention setting must clean the DB row and the on-disk directory
    together, so an expired job disappears from the job history, not just
    from the download folder."""

    def test_expired_job_is_removed_from_db_and_disk(self) -> None:
        from app.main import _run_housekeeping_once

        expired_id = str(uuid.uuid4())
        kept_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, expired_id, days_old=10)
        _insert_finished_job(db.DB_PATH, kept_id, days_old=1)

        data_dir = self._tmp_data_dir()
        expired_dir = data_dir / expired_id
        kept_dir = data_dir / kept_id
        expired_dir.mkdir(parents=True)
        kept_dir.mkdir(parents=True)
        (expired_dir / "video.mp4").write_bytes(b"x")
        (kept_dir / "video.mp4").write_bytes(b"x")

        db.set_settings({"retention_days": 7})

        with patch("app.main.DATA_DIR", data_dir):
            _run_housekeeping_once()

        # DB row is gone -> disappears from job history, not just downloads.
        self.assertCountEqual(db.list_job_ids(), [kept_id])
        # Filesystem artifacts follow the DB row.
        self.assertFalse(expired_dir.exists())
        self.assertTrue(kept_dir.exists())

    def test_zero_retention_purges_nothing(self) -> None:
        from app.main import _run_housekeeping_once

        job_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, job_id, days_old=400)

        data_dir = self._tmp_data_dir()
        job_dir = data_dir / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "video.mp4").write_bytes(b"x")

        db.set_settings({"retention_days": 0})

        with patch("app.main.DATA_DIR", data_dir):
            _run_housekeeping_once()

        self.assertCountEqual(db.list_job_ids(), [job_id])
        self.assertTrue(job_dir.exists())

    def test_expired_job_share_link_is_removed(self) -> None:
        from app.main import _run_housekeeping_once

        expired_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, expired_id, days_old=10)
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                ("share-token", expired_id, 0),
            )
            connection.commit()

        data_dir = self._tmp_data_dir()
        db.set_settings({"retention_days": 7})

        with patch("app.main.DATA_DIR", data_dir):
            _run_housekeeping_once()

        with sqlite3.connect(db.DB_PATH) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM share_links WHERE token = ?",
                ("share-token",),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def _tmp_data_dir(self):
        # IsolatedDbTestCase already provides a per-test tempdir at self._tmp;
        # reuse it for job directories instead of creating a second one.
        data_dir = Path(self._tmp.name) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir


if __name__ == "__main__":
    import unittest

    unittest.main()
