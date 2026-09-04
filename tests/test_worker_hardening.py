#!/usr/bin/env python3
#
# tests/test_worker_hardening.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Failure modes of the worker that are easy to hit and hard to see.

Each case here is one a job either dies from or leaks through: a title that
does not fit on disk, credentials quoted back in an error message, a worker
pool that starts a second time on top of itself.
"""

import threading
import unittest

from app import worker
from app.worker import _redact_urls, sanitize_filename


class SanitizeFilenameTests(unittest.TestCase):
    def test_plain_titles_pass_through(self):
        self.assertEqual(sanitize_filename("Harald Juhnke - Barfuss"), "Harald Juhnke - Barfuss")

    def test_path_and_reserved_characters_are_replaced(self):
        self.assertEqual(sanitize_filename("a/b:c*d"), "a_b_c_d")
        self.assertEqual(sanitize_filename("CON"), "video")
        self.assertEqual(sanitize_filename("   "), "video")

    def test_a_multibyte_title_stays_within_the_filesystem_limit(self):
        # 120 CJK characters are ~360 bytes; NAME_MAX is 255. Truncating by
        # character count alone made the whole job fail with ENAMETOOLONG.
        title = "あ" * 200
        cleaned = sanitize_filename(title)
        self.assertLessEqual(len(cleaned.encode("utf-8")), worker._STEM_MAX_BYTES)

    def test_the_budget_leaves_room_for_what_the_callers_append(self):
        stem = f"{sanitize_filename('あ' * 200)} ({worker._quality_label('max')})"
        # Worst case on the way to disk: the temp file of the finalize pass.
        longest = f"{stem}.finalized.webm".encode()
        self.assertLess(len(longest), 255)

    def test_ascii_titles_are_still_capped_by_character_count(self):
        self.assertEqual(len(sanitize_filename("a" * 300)), 120)


class RedactUrlsTests(unittest.TestCase):
    def test_signed_query_strings_are_dropped(self):
        tail = "ERROR: unable to download https://rr3.googlevideo.com/videoplayback?sig=SECRET&pot=TOKEN"
        redacted = _redact_urls(tail)
        self.assertNotIn("SECRET", redacted)
        self.assertNotIn("TOKEN", redacted)
        # The part that makes the error readable survives.
        self.assertIn("https://rr3.googlevideo.com/videoplayback?<redacted>", redacted)

    def test_urls_without_a_query_string_are_left_alone(self):
        tail = "ERROR: https://example.com/video.mp4 not found"
        self.assertEqual(_redact_urls(tail), tail)

    def test_text_without_urls_is_unchanged(self):
        self.assertEqual(_redact_urls("ffmpeg: no such file"), "ffmpeg: no such file")


class WorkerLifecycleTests(unittest.TestCase):
    """stop_workers() must not hide threads that outlived the join."""

    def tearDown(self):
        with worker._worker_lock:
            worker._worker_threads.clear()
            worker._workers_started = False
        worker._shutdown_event.clear()

    def test_a_thread_that_survives_the_join_keeps_the_pool_marked_started(self):
        release = threading.Event()
        survivor = threading.Thread(target=release.wait, daemon=True)
        survivor.start()
        try:
            with worker._worker_lock:
                worker._worker_threads[:] = [survivor]
                worker._workers_started = True

            worker.stop_workers(timeout=0.0)

            # Still visible, so start_workers() takes its "already started"
            # early return instead of clearing the shutdown event and running a
            # second pool over the same job directories.
            self.assertIn(survivor, worker._worker_threads)
            self.assertTrue(worker._workers_started)
        finally:
            release.set()
            survivor.join(timeout=2)

    def test_threads_that_stopped_cleanly_are_forgotten(self):
        finished = threading.Thread(target=lambda: None, daemon=True)
        finished.start()
        finished.join(timeout=2)
        with worker._worker_lock:
            worker._worker_threads[:] = [finished]
            worker._workers_started = True

        worker.stop_workers(timeout=0.0)

        self.assertEqual(worker._worker_threads, [])
        self.assertFalse(worker._workers_started)


if __name__ == "__main__":
    unittest.main()
