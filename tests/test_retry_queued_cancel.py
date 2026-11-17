#!/usr/bin/env python3
"""A job cancelled while queued and then retried keeps exactly one queue entry."""

import queue
import unittest
from unittest.mock import patch

from app.workers import worker

JOB = ("22222222-2222-2222-2222-222222222222", "https://example.com/v", "audio", "max")


class RetryAfterQueuedCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        for target, value in (("_job_queue", queue.Queue(maxsize=4)), ("_queued_job_ids", set())):
            patcher = patch.object(worker, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(worker.clear_cancellation, JOB[0])

    def test_retry_does_not_enqueue_a_second_entry(self) -> None:
        self.assertTrue(worker.submit_download(JOB))
        worker.cancel_job(JOB[0])  # cancel leaves the entry and its slot in place
        worker.clear_cancellation(JOB[0])  # what retry_job does before resubmitting
        self.assertTrue(worker.submit_download(JOB))
        self.assertEqual(worker.get_job_queue().qsize(), 1)
        self.assertFalse(worker.is_job_cancelled(JOB[0]))


if __name__ == "__main__":
    unittest.main()
