#!/usr/bin/env python3
#
# tests/test_submit_duplicates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The ``409 duplicate_job`` contract has to hold under concurrency: the
duplicate check and the insert are one atomic database step."""

import threading
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app import db
from tests._support import IsolatedDbTestCase, WebAppTestCase

_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _insert(*, allow_duplicate: bool = False):
    return db.insert_job_unless_active_duplicate(
        str(uuid.uuid4()),
        _URL,
        "audio",
        "max",
        "queued",
        allow_duplicate=allow_duplicate,
    )


class AtomicDuplicateInsertTests(IsolatedDbTestCase):
    def test_parallel_identical_inserts_create_exactly_one_job(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)
        results: list[object] = []

        def run() -> None:
            barrier.wait()
            results.append(_insert())

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for r in results if r is None), 1)
        self.assertEqual(len(db.list_job_ids()), 1)
        blockers = [r for r in results if r is not None]
        self.assertEqual(len(blockers), workers - 1)
        self.assertEqual({row["id"] for row in blockers}, set(db.list_job_ids()))

    def test_confirmed_duplicate_is_allowed(self) -> None:
        self.assertIsNone(_insert())
        self.assertIsNotNone(_insert())
        self.assertIsNone(_insert(allow_duplicate=True))
        self.assertEqual(len(db.list_job_ids()), 2)

    def test_errored_job_does_not_block_a_retry(self) -> None:
        db.insert_job(str(uuid.uuid4()), _URL, "audio", "max", "error")
        self.assertIsNone(_insert())

    def test_other_quality_is_not_a_duplicate(self) -> None:
        self.assertIsNone(_insert())
        other = db.insert_job_unless_active_duplicate(str(uuid.uuid4()), _URL, "audio", "small", "queued")
        self.assertIsNone(other)


class SubmitDuplicateRouteTests(WebAppTestCase):
    def _submit(self, **extra: object):
        data = {"url": _URL, "type": "audio", "quality": "max", **extra}
        return self.client.post("/api/submit", data=data, headers={"X-CSRF-Token": self._csrf()})

    def _patches(self, *, queue_accepts: bool = True):
        return (
            patch(
                "app.routes.api.get_job_queue",
                return_value=MagicMock(full=lambda: False),
            ),
            patch("app.routes.api.governor.can_accept_job_async", return_value=True),
            patch(
                "app.routes.api.extract_video_meta_async",
                new=AsyncMock(side_effect=RuntimeError("skip")),
            ),
            patch("app.routes.api.submit_download", return_value=queue_accepts),
        )

    def test_precheck_race_still_ends_in_409(self) -> None:
        """A submit whose fast-path check saw nothing is stopped by the atomic one."""
        first, second, third, fourth = self._patches()
        with first, second, third, fourth:
            self.assertEqual(self._submit().status_code, 200)
            with patch("app.routes.api.find_active_job_for_submission", return_value=None):
                response = self._submit()
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["detail"], "duplicate_job")
        self.assertEqual(body["existing_job"]["url"], _URL)
        self.assertEqual(len(db.list_job_ids()), 1)

    def test_confirm_duplicate_creates_second_job(self) -> None:
        first, second, third, fourth = self._patches()
        with first, second, third, fourth:
            self.assertEqual(self._submit().status_code, 200)
            self.assertEqual(self._submit(confirm_duplicate="true").status_code, 200)
        self.assertEqual(len(db.list_job_ids()), 2)

    def test_queue_overflow_leaves_no_blocking_row(self) -> None:
        first, second, third, fourth = self._patches(queue_accepts=False)
        with first, second, third, fourth:
            self.assertEqual(self._submit().status_code, 503)
        rows = [db.get_job(job_id) for job_id in db.list_job_ids()]
        self.assertEqual([row["status"] for row in rows], ["error"])
        first, second, third, fourth = self._patches()
        with first, second, third, fourth:
            self.assertEqual(self._submit().status_code, 200)

    def test_headroom_is_checked_before_metadata_lookup(self) -> None:
        meta = AsyncMock(return_value={})
        with (
            patch(
                "app.routes.api.get_job_queue",
                return_value=MagicMock(full=lambda: False),
            ),
            patch("app.routes.api.governor.can_accept_job_async", return_value=False),
            patch("app.routes.api.extract_video_meta_async", new=meta),
        ):
            response = self._submit()
        self.assertEqual(response.status_code, 503)
        meta.assert_not_called()


if __name__ == "__main__":
    unittest.main()
