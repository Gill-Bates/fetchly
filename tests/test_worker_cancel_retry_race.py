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

import threading
from unittest.mock import patch

from app import worker
from app.db import get_job, insert_job, update_job, utc_timestamp
from app.governor import GovernorConfig
from tests._support import IsolatedDbTestCase, WebAppTestCase

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


class RetryRejectedWhileWorkerStillOwnsTheJobTests(IsolatedDbTestCase):
    """N1: a job blocked on the transcode semaphore has no subprocess to kill,
    so a cancel there leaves the old worker thread with nothing to stop it -
    the in-memory ownership registry (worker.is_job_active) is what a retry
    must consult instead. See app/routes/api.py::retry_job.
    """

    def setUp(self) -> None:
        super().setUp()
        self.job_id = JOB[0]
        patcher = patch.object(worker, "_status_callback", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker._shutdown_event.clear()

        # get_job_queue()/worker() read governor.queue_maxsize; configure() is
        # idempotent, so this only takes effect the first time any test in
        # the process calls it, but it guarantees a configured governor
        # regardless of test order.
        worker.governor.configure(GovernorConfig(queue_maxsize=4))

        # A fresh, empty job queue: the module-level singleton would otherwise
        # carry state (or a maxsize) left over from an earlier test.
        queue_patcher = patch.object(worker, "_job_queue", None)
        queue_patcher.start()
        self.addCleanup(queue_patcher.stop)

    def tearDown(self) -> None:
        worker._shutdown_event.clear()
        with worker._active_worker_jobs_lock:
            worker._active_worker_jobs.clear()
        with worker._cancel_lock:
            worker._cancelled_jobs.clear()
        with worker._queued_ids_lock:
            worker._queued_job_ids.clear()
        super().tearDown()

    def test_retry_is_rejected_while_a_worker_thread_still_owns_the_job(self) -> None:
        insert_job(*JOB, "queued")

        download_started = threading.Event()
        release_worker = threading.Event()

        def blocked_on_semaphore(*_args, **_kwargs):
            # Simulates the N1 race: the attempt has already downloaded and is
            # now blocked waiting for a transcode slot, with no subprocess
            # left for cancel_job() to terminate.
            download_started.set()
            release_worker.wait(timeout=5)
            raise worker.JobCancelledError("cancelled")

        with patch.object(worker, "_download_media", side_effect=blocked_on_semaphore):
            thread = threading.Thread(target=worker.worker, daemon=True)
            thread.start()
            try:
                self.assertTrue(worker.submit_download(JOB))
                self.assertTrue(download_started.wait(timeout=5), "worker never reached the blocking point")

                # A cancel arriving now writes the row straight to "cancelled"
                # and sets the marker, exactly like app/routes/api.py::cancel_job -
                # there is no active subprocess for it to terminate.
                update_job(self.job_id, status="cancelled", finished_at=utc_timestamp())
                worker.cancel_job(self.job_id)

                # The row already reads "cancelled", which is what a naive
                # retry guard (checking only the DB status) would accept - the
                # registry is what must still refuse it.
                job = get_job(self.job_id)
                assert job is not None
                self.assertEqual(job["status"], "cancelled")
                self.assertTrue(
                    worker.is_job_active(self.job_id),
                    "the worker thread is still inside process_job(); retry must not re-enqueue this job",
                )
            finally:
                release_worker.set()
                worker.signal_shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        # Once the old attempt has fully unwound, the registry entry is gone
        # and a retry is safe again.
        self.assertFalse(worker.is_job_active(self.job_id))


class RetryEndpointRejectsActiveJobsTests(WebAppTestCase):
    """The HTTP-level counterpart to RetryRejectedWhileWorkerStillOwnsTheJobTests:

    proves POST /api/jobs/{id}/retry itself returns 409 while worker.is_job_active()
    is true, not just that the internal registry function reports it correctly.
    """

    def setUp(self) -> None:
        super().setUp()
        self.job_id = JOB[0]
        insert_job(*JOB, "cancelled")
        with worker._active_worker_jobs_lock:
            worker._active_worker_jobs.add(self.job_id)
        self.addCleanup(self._clear_active)

    def _clear_active(self) -> None:
        with worker._active_worker_jobs_lock:
            worker._active_worker_jobs.discard(self.job_id)

    def test_retry_returns_409_while_the_registry_still_owns_the_job(self) -> None:
        response = self.client.post(
            f"/api/jobs/{self.job_id}/retry",
            headers={"X-CSRF-Token": self._csrf()},
        )

        self.assertEqual(response.status_code, 409)

        # The row itself must be untouched - no second worker was ever
        # allowed to reset it into "queued".
        job = get_job(self.job_id)
        assert job is not None
        self.assertEqual(job["status"], "cancelled")

    def test_retry_succeeds_once_the_registry_entry_is_gone(self) -> None:
        self._clear_active()

        response = self.client.post(
            f"/api/jobs/{self.job_id}/retry",
            headers={"X-CSRF-Token": self._csrf()},
        )

        self.assertEqual(response.status_code, 200)
        job = get_job(self.job_id)
        assert job is not None
        self.assertEqual(job["status"], "queued")
