#!/usr/bin/env python3
#
# tests/test_template_filters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest

from app.db import to_iso_utc
from app.routes.api import job_to_dict
from app.utils.template_filters import filesize, localtime


class FileSizeFilterTests(unittest.TestCase):
    def test_gib_values_use_one_decimal_place(self) -> None:
        gib = 1_073_741_824

        self.assertEqual(str(filesize(gib * 1_567 // 10)), "156.7 GiB")
        self.assertEqual(str(filesize(gib * 1_568 // 10)), "156.8 GiB")


class UtcTimestampTests(unittest.TestCase):
    """The server only ever hands out UTC instants; the browser localizes them."""

    def test_stored_naive_text_is_read_as_utc(self) -> None:
        self.assertEqual(to_iso_utc("2026-01-02 03:04:05"), "2026-01-02T03:04:05Z")

    def test_explicit_offsets_are_converted_to_utc(self) -> None:
        self.assertEqual(to_iso_utc("2026-01-02T03:04:05+02:00"), "2026-01-02T01:04:05Z")

    def test_missing_or_garbage_values_yield_none(self) -> None:
        for value in (None, "", "yesterday"):
            self.assertIsNone(to_iso_utc(value))

    def test_job_payload_carries_iso_utc_timestamps(self) -> None:
        job = dict.fromkeys(
            (
                "url",
                "video_title",
                "video_meta_hover",
                "type",
                "quality",
                "message",
                "filesize_bytes",
                "duration_seconds",
                "codec",
                "bitrate_kbps",
                "bpm",
                "bpm_confidence",
                "audio_hash",
                "filename",
            )
        )
        job |= {"id": "job-1", "status": "done", "created_at": "2026-01-02 03:04:05", "finished_at": None}
        payload = job_to_dict(job)
        self.assertEqual(payload["created_at"], "2026-01-02T03:04:05Z")
        self.assertIsNone(payload["finished_at"])

    def test_localtime_filter_emits_a_utc_time_element_for_the_browser(self) -> None:
        html = str(localtime("2026-01-02 03:04:05"))
        self.assertIn('<time datetime="2026-01-02T03:04:05Z" data-localize>', html)
        self.assertIn("03:04 UTC", html)
        self.assertEqual(str(localtime(None)), "")
        self.assertEqual(str(localtime("<b>")), "&lt;b&gt;")
