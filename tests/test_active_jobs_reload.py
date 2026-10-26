#!/usr/bin/env python3
#
# tests/test_active_jobs_reload.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A reloaded dashboard must find running jobs again, with their last progress."""

import json
import re
import uuid

from app import db
from app.routes.api import job_to_dict
from app.workers import worker
from tests._support import IsolatedDbTestCase, WebAppTestCase

_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _insert(status: str, *, history: bool = True) -> str:
    job_id = str(uuid.uuid4())
    db.insert_job(job_id, _URL, "video", "max", status, include_in_history=history)
    return job_id


class ListActiveJobsTests(IsolatedDbTestCase):
    def test_returns_only_non_terminal_jobs_even_without_history(self) -> None:
        active = {
            _insert("queued"),
            _insert("downloading", history=False),
            _insert("transcoding"),
            _insert("analysis"),
        }
        _insert("done")
        _insert("error")
        _insert("cancelled")

        self.assertEqual({row["id"] for row in db.list_active_jobs()}, active)
        self.assertNotIn(
            next(row["id"] for row in db.list_active_jobs() if row["status"] == "downloading"),
            {row["id"] for row in db.paginate_jobs()},
        )


class ProgressSnapshotTests(IsolatedDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(worker._progress_snapshots.clear)

    def test_running_job_carries_last_progress_until_status_leaves_it(self) -> None:
        job_id = _insert("downloading")
        worker._emit(job_id, "downloading", "x", progress=42, eta_seconds=7)

        row = db.get_job(job_id)
        data = job_to_dict(row)
        self.assertEqual((data["progress"], data["eta_seconds"]), (42, 7))

        worker._emit(job_id, "transcoding", "y")
        db.update_job(job_id, status="transcoding")
        self.assertNotIn("progress", job_to_dict(db.get_job(job_id)))

        worker._emit(job_id, "transcoding", "z", progress=10, eta_seconds=None)
        worker._emit(job_id, "done", "ok")
        db.update_job(job_id, status="done")
        self.assertNotIn("progress", job_to_dict(db.get_job(job_id)))


class IndexBootstrapTests(WebAppTestCase):
    def test_index_embeds_active_jobs_missing_from_history(self) -> None:
        hidden = _insert("downloading", history=False)
        finished = _insert("done")

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'<script id="activeJobsBootstrapData"[^>]*>(.*?)</script>', response.text, re.DOTALL)
        assert match is not None
        self.assertEqual([job["id"] for job in json.loads(match.group(1))], [hidden])
        self.assertNotIn(
            hidden, re.search(r'id="jobsBootstrapData"[^>]*>(.*?)</script>', response.text, re.DOTALL).group(1)
        )
        self.assertIn(finished, response.text)
