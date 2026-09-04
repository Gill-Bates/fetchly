#!/usr/bin/env python3
#
# tests/test_job_duration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The runtime a job shows: read at submit, refined by ffprobe, never lost."""

import unittest
from unittest.mock import patch

from app import db
from app.utils.duration import format_clock
from app.utils.youtube import extract_video_meta
from tests._support import IsolatedDbTestCase


class InsertJobDurationTests(IsolatedDbTestCase):
    def _insert(self, job_id: str, duration: object) -> None:
        db.insert_job(
            job_id,
            "https://example.com/video",
            "video",
            "max",
            "queued",
            duration_seconds=duration,
        )

    def test_source_duration_is_stored_with_the_queued_job(self) -> None:
        self._insert("job-known", 15690.44)
        job = db.get_job("job-known")
        assert job is not None
        # Stored at the app's working precision, not the raw reading.
        self.assertEqual(job["duration_seconds"], 15690.4)

    def test_unusable_durations_are_stored_as_unknown(self) -> None:
        for index, value in enumerate([None, 0, -1, float("nan"), "4:21:30"]):
            with self.subTest(value=value):
                job_id = f"job-unknown-{index}"
                self._insert(job_id, value)
                job = db.get_job(job_id)
                assert job is not None
                self.assertIsNone(job["duration_seconds"])

    def test_ffprobe_reading_replaces_the_source_value(self) -> None:
        self._insert("job-refined", 261 * 60)
        db.update_job("job-refined", duration_seconds=15690.4)
        job = db.get_job("job-refined")
        assert job is not None
        self.assertEqual(job["duration_seconds"], 15690.4)


class ExtractVideoMetaDurationTests(unittest.TestCase):
    def _meta(self, info: dict[str, object] | None) -> dict[str, object]:
        with patch("app.utils.youtube.load_video_info", return_value=info):
            return extract_video_meta("https://example.com/video")

    def test_duration_is_returned_next_to_the_hover_text(self) -> None:
        meta = self._meta({"title": "A long mix", "duration": 15690})
        self.assertEqual(meta["duration_seconds"], 15690)
        self.assertIn("Duration: 4:21:30", str(meta["video_meta_hover"]))

    def test_missing_or_zero_duration_stays_none(self) -> None:
        self.assertIsNone(self._meta({"title": "No length"})["duration_seconds"])
        self.assertIsNone(self._meta({"title": "Live", "duration": 0})["duration_seconds"])

    def test_unavailable_source_still_answers_with_every_key(self) -> None:
        meta = self._meta(None)
        self.assertEqual(
            meta,
            {"video_title": None, "video_meta_hover": None, "duration_seconds": None},
        )


class FormatClockTests(unittest.TestCase):
    """Must agree with formatDuration() in app/static/js/utils.js."""

    def test_renders_hours_only_when_there_are_any(self) -> None:
        self.assertEqual(format_clock(15690), "4:21:30")
        self.assertEqual(format_clock(213.4), "3:33")
        self.assertEqual(format_clock(59), "0:59")

    def test_unknown_values_render_as_the_en_dash(self) -> None:
        for value in (None, -1, float("inf"), "abc", True):
            with self.subTest(value=value):
                self.assertEqual(format_clock(value), "–")


if __name__ == "__main__":
    unittest.main()
