#!/usr/bin/env python3
#
# tests/test_job_paths.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The central per-job path helper: DATA_DIR/jobs/<uuid>, UUID-validated.

Guards the 1.3.2 layout invariant - every per-job directory lives under
DATA_DIR/jobs/<uuid>, the helper rejects anything that is not a UUID (no
``../`` traversal, no non-job name), and there is no injectable root: both
helpers derive solely from the global DATA_DIR, so a test redirects them
only through the DATA_DIR env var, the same path model production uses.
"""

import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.utils.fs import get_data_dir, job_dir, jobs_dir


class JobsDirTests(unittest.TestCase):
    def test_jobs_dir_is_the_jobs_subfolder_of_data_dir(self) -> None:
        self.assertEqual(jobs_dir(), get_data_dir() / "jobs")

    def test_jobs_dir_tracks_the_global_data_dir_env_var(self) -> None:
        root = Path("/tmp/fetchly-test-root")
        with patch.dict(os.environ, {"DATA_DIR": str(root)}):
            self.assertEqual(jobs_dir(), root.resolve() / "jobs")


class JobDirTests(unittest.TestCase):
    def test_canonical_uuid_resolves_under_jobs(self) -> None:
        job_id = str(uuid.uuid4())
        self.assertEqual(job_dir(job_id), get_data_dir() / "jobs" / job_id)

    def test_job_dir_tracks_the_global_data_dir_env_var(self) -> None:
        job_id = str(uuid.uuid4())
        root = Path("/tmp/fetchly-test-root")
        with patch.dict(os.environ, {"DATA_DIR": str(root)}):
            self.assertEqual(job_dir(job_id), root.resolve() / "jobs" / job_id)

    def test_uuid_spelling_is_canonicalized(self) -> None:
        raw = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        expected = str(uuid.UUID(raw))
        self.assertEqual(job_dir(raw).name, expected)
        self.assertNotEqual(job_dir(raw).name, raw)

    def test_accepts_a_uuid_object(self) -> None:
        job_uuid = uuid.uuid4()
        self.assertEqual(job_dir(job_uuid).name, str(job_uuid))

    def test_traversal_attempt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            job_dir("../cookies")

    def test_non_uuid_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            job_dir("not-a-uuid")

    def test_empty_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            job_dir("")


if __name__ == "__main__":
    unittest.main()
