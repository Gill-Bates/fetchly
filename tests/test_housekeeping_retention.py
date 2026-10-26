#!/usr/bin/env python3
#
# tests/test_housekeeping_retention.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Retention removes files first; only jobs whose files are gone lose their row."""

import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from time import time
from unittest.mock import patch

from app import db
from app.utils.housekeeping import cleanup_watermark_cache
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

    def test_zero_keep_days_keeps_everything_terminal(self) -> None:
        job_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, job_id, days_old=1)

        deleted = db.purge_old_jobs(0)

        self.assertEqual(deleted, [])
        self.assertEqual(db.list_job_ids(), [job_id])

    def test_non_terminal_jobs_are_never_purged(self) -> None:
        queued_id = str(uuid.uuid4())
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO jobs (id, url, type, status) VALUES (?, ?, ?, ?)",
                (queued_id, "https://example.com/video", "video", "queued"),
            )
            connection.commit()

        deleted = db.purge_old_jobs(7)

        self.assertEqual(deleted, [])
        self.assertCountEqual(db.list_job_ids(), [queued_id])


class CacheTtlTests(unittest.TestCase):
    def test_watermark_cache_drops_only_expired_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            old, fresh = cache / "old.png", cache / "fresh.png"
            old.write_bytes(b"x")
            fresh.write_bytes(b"x")
            now = time()
            os.utime(old, (now - 8 * 86400, now - 8 * 86400))

            removed = cleanup_watermark_cache(cache, now=now)

            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_missing_watermark_cache_is_a_no_op(self) -> None:
        self.assertEqual(cleanup_watermark_cache(Path("/nonexistent/watermark-cache")), 0)


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
        jobs_root = data_dir / "jobs"
        expired_dir = jobs_root / expired_id
        kept_dir = jobs_root / kept_id
        expired_dir.mkdir(parents=True)
        kept_dir.mkdir(parents=True)
        (expired_dir / "video.mp4").write_bytes(b"x")
        (kept_dir / "video.mp4").write_bytes(b"x")

        db.set_settings({"retention_days": 7})

        with patch.dict(os.environ, {"DATA_DIR": str(data_dir)}):
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
        job_dir = data_dir / "jobs" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "video.mp4").write_bytes(b"x")

        db.set_settings({"retention_days": 0})

        with patch.dict(os.environ, {"DATA_DIR": str(data_dir)}):
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

        with patch.dict(os.environ, {"DATA_DIR": str(data_dir)}):
            _run_housekeeping_once()

        with sqlite3.connect(db.DB_PATH) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM share_links WHERE token = ?",
                ("share-token",),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_failed_directory_cleanup_keeps_the_job_for_a_retry(self) -> None:
        from app.main import _run_housekeeping_once

        expired_id = str(uuid.uuid4())
        _insert_finished_job(db.DB_PATH, expired_id, days_old=10)
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO share_links (token, job_id, max_uses) VALUES (?, ?, ?)",
                ("retained-token", expired_id, 1),
            )
            connection.commit()

        data_dir = self._tmp_data_dir()
        expired_dir = data_dir / "jobs" / expired_id
        expired_dir.mkdir(parents=True)
        db.set_settings({"retention_days": 7})

        with (
            patch.dict(os.environ, {"DATA_DIR": str(data_dir)}),
            patch("app.utils.housekeeping.cleanup_job_directory", return_value=False),
        ):
            _run_housekeeping_once()

        # The files survived, so the row and its share link must too.
        self.assertEqual(db.list_job_ids(), [expired_id])
        self.assertIsNotNone(db.get_share_link("retained-token"))
        self.assertTrue(expired_dir.exists())

        with patch.dict(os.environ, {"DATA_DIR": str(data_dir)}):
            _run_housekeeping_once()

        self.assertEqual(db.list_job_ids(), [])
        self.assertIsNone(db.get_share_link("retained-token"))
        self.assertFalse(expired_dir.exists())

    def test_cleanup_returns_only_successfully_cleaned_ids(self) -> None:
        from app.utils.housekeeping import cleanup_expired_jobs

        good_id = str(uuid.uuid4())
        bad_id = str(uuid.uuid4())
        data_dir = self._tmp_data_dir()
        (data_dir / good_id).mkdir()
        (data_dir / bad_id).mkdir()

        def fake_cleanup(job_id: str, _data_dir: Path) -> bool:
            return job_id == good_id

        with patch("app.utils.housekeeping.cleanup_job_directory", side_effect=fake_cleanup):
            cleaned = cleanup_expired_jobs(7, data_dir, lambda _days: [good_id, bad_id, "not-a-uuid"])

        self.assertEqual(cleaned, [good_id])

    def _tmp_data_dir(self):
        # IsolatedDbTestCase already provides a per-test tempdir at self._tmp;
        # reuse it for job directories instead of creating a second one.
        data_dir = Path(self._tmp.name) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir


class OrphanSweepJobsLayoutTests(unittest.TestCase):
    """The orphan sweep scans DATA_DIR/jobs/ for per-job UUID directories.

    A directory with no matching job row is removed; a sibling cache dir that
    happens to sit next to jobs/ is never seen because the sweep roots at
    jobs/, and a non-UUID name inside jobs/ is left alone.
    """

    def test_orphan_under_jobs_is_swept_and_siblings_are_untouched(self) -> None:
        from app.utils.housekeeping import cleanup_orphaned_directories

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            jobs_root = data_dir / "jobs"
            jobs_root.mkdir()

            orphan_id = str(uuid.uuid4())
            orphan_dir = jobs_root / orphan_id
            orphan_dir.mkdir()
            # Pre-date the grace period so the sweep acts on it.
            old = time() - 3600
            os.utime(orphan_dir, (old, old))

            # A non-UUID directory inside jobs/ must be left alone.
            not_a_job = jobs_root / "not-a-uuid"
            not_a_job.mkdir()
            os.utime(not_a_job, (old, old))

            # Sibling caches live next to jobs/, not under it, so the sweep
            # rooted at jobs/ can never reach them.
            for sibling in (
                ".cache",
                "cookies",
                "thumb-cache",
                "watermark-cache",
                "geolite2",
            ):
                (data_dir / sibling).mkdir()

            removed = cleanup_orphaned_directories(jobs_root, lambda _id: False)

            self.assertEqual(removed, [orphan_id])
            self.assertFalse(orphan_dir.exists())
            self.assertTrue(not_a_job.exists())
            for sibling in (
                ".cache",
                "cookies",
                "thumb-cache",
                "watermark-cache",
                "geolite2",
            ):
                self.assertTrue((data_dir / sibling).exists())

    def test_orphan_with_matching_job_row_is_kept(self) -> None:
        from app.utils.housekeeping import cleanup_orphaned_directories

        with tempfile.TemporaryDirectory() as tmp:
            jobs_root = Path(tmp) / "jobs"
            jobs_root.mkdir()
            kept_id = str(uuid.uuid4())
            kept_dir = jobs_root / kept_id
            kept_dir.mkdir()
            old = time() - 3600
            os.utime(kept_dir, (old, old))

            removed = cleanup_orphaned_directories(jobs_root, lambda job_id: job_id == kept_id)

            self.assertEqual(removed, [])
            self.assertTrue(kept_dir.exists())


if __name__ == "__main__":
    unittest.main()
