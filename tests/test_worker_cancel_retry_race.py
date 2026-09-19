#!/usr/bin/env python3
#
# tests/test_worker_cancel_retry_race.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Regression tests for the conditional job writebacks in app/worker.py.

A cancel and a retry can both move a job row while a worker thread is still
inside ``process_job()``. The worker's own terminal writes therefore have to be
conditional, or an observation made before the retry lands would overwrite the
fresh attempt (ABA).
"""

from unittest.mock import patch

from app import worker
from app.db import get_job, insert_job
from tests._support import IsolatedDbTestCase

JOB = ("11111111-1111-1111-1111-111111111111", "https://example.com/v", "audio", "max")


class CancelledWritebackIsConditionalTests(IsolatedDbTestCase):
    """A stale cancel must never land on a row that was retried meanwhile."""

    def setUp(self) -> None:
        super().setUp()
        self.job_id = JOB[0]
        patcher = patch.object(worker, "_status_callback", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _process_a_cancelled_download(self, status: str) -> None:
        """Insert a job in *status* and drive process_job() into its cancel path."""
        insert_job(*JOB, status)

        def raise_cancelled(*_args, **_kwargs):
            raise worker.JobCancelledError("cancelled")

        with patch.object(worker, "_download_media", side_effect=raise_cancelled):
            worker.process_job(JOB)

    def test_a_job_still_owned_by_the_worker_is_cancelled(self) -> None:
        self._process_a_cancelled_download("downloading")

        job = get_job(self.job_id)
        assert job is not None
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNotNone(job["finished_at"])

    def test_a_retried_job_is_not_cancelled_again(self) -> None:
        # The state a retry leaves behind: the row is back to "queued" and
        # re-enqueued while the old worker thread is still unwinding.
        self._process_a_cancelled_download("queued")

        job = get_job(self.job_id)
        assert job is not None
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["finished_at"])
