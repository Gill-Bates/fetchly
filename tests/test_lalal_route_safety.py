#!/usr/bin/env python3
#
# tests/test_lalal_route_safety.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.routes import lalal


class LalalRouteSafetyTests(unittest.TestCase):
    def test_capacity_exhaustion_returns_retryable_error(self) -> None:
        async def check() -> HTTPException:
            original_capacity = lalal._processing_capacity
            try:
                lalal._processing_capacity = asyncio.Semaphore(0)
                with patch.object(lalal, "_LALAL_CAPACITY_WAIT_SECONDS", 0.01):
                    with self.assertRaises(HTTPException) as raised:
                        async with lalal._processing_capacity_slot():
                            self.fail("unreachable")
                return raised.exception
            finally:
                lalal._processing_capacity = original_capacity

        error = asyncio.run(check())
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.headers, {"Retry-After": "5"})

    def test_capacity_slot_is_released_after_processing(self) -> None:
        async def check() -> None:
            original_capacity = lalal._processing_capacity
            try:
                capacity = asyncio.Semaphore(1)
                lalal._processing_capacity = capacity
                async with lalal._processing_capacity_slot():
                    self.assertTrue(capacity.locked())
                self.assertFalse(capacity.locked())
            finally:
                lalal._processing_capacity = original_capacity

        asyncio.run(check())

    def test_processing_attempt_cleans_outputs_before_releasing_lock(self) -> None:
        async def check(lock_file: Path, output_path: Path) -> None:
            with self.assertRaisesRegex(RuntimeError, "split failed"):
                async with lalal._processing_attempt(lock_file, output_path):
                    raise RuntimeError("split failed")
            self.assertFalse(output_path.exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "vocals.mp3"
            output_path.write_bytes(b"partial")
            asyncio.run(check(root / ".lalal.lock", output_path))

    def test_mp3_stem_is_published_by_atomic_rename(self) -> None:
        async def check(source: Path, target: Path) -> None:
            await lalal._finalize_stem(source, target)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "returned.mp3"
            target = root / "vocals.mp3"
            source.write_bytes(b"complete stem")

            asyncio.run(check(source, target))

            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"complete stem")


if __name__ == "__main__":
    unittest.main()
