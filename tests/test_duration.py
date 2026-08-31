#!/usr/bin/env python3
#
# tests/test_duration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Route imports load the session module, which intentionally requires these
# values at application startup. Dummy values keep this unit test isolated.
os.environ.setdefault("FETCHLY_SECRET_KEY", "test-duration-secret")
os.environ.setdefault("FETCHLY_ADMIN_PASSWORD", "test-duration-password")

from app import db
from app.routes.trim import _duration_validation_error
from app.utils.duration import format_seconds, round_seconds
from app.worker import _probe_media


class DurationRoundingTests(unittest.TestCase):
    def test_rounds_finite_numeric_values_to_one_decimal(self) -> None:
        self.assertEqual(round_seconds(213.44), 213.4)
        self.assertEqual(round_seconds(213.46), 213.5)
        self.assertEqual(round_seconds(213), 213.0)

    def test_rejects_non_numeric_and_non_finite_values(self) -> None:
        for value in (None, True, "213.4", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(round_seconds(value))

    def test_formats_whole_and_fractional_seconds(self) -> None:
        self.assertEqual(format_seconds(213.0), "213")
        self.assertEqual(format_seconds(213.44), "213.4")


class DurationPipelineTests(unittest.TestCase):
    def test_probe_preserves_one_decimal_duration(self) -> None:
        payload = json.dumps({
            "format": {"duration": "213.44", "bit_rate": "128000"},
            "streams": [{"codec_type": "audio", "codec_name": "opus"}],
        })
        with patch("app.worker._run_cmd", return_value=payload):
            codec, bitrate_kbps, duration_seconds = _probe_media(Path("track.opus"), "audio")

        self.assertEqual(codec, "opus")
        self.assertEqual(bitrate_kbps, 128)
        self.assertEqual(duration_seconds, 213.4)

    def test_trim_duration_guard_accepts_the_rounded_track_end(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT 213.4 AS duration_seconds").fetchone()
        self.assertIsNotNone(row)

        # sqlite3.Row membership checks values, not column names. The route
        # therefore passes the value directly to the shared duration guard.
        self.assertNotIn("duration_seconds", row)
        self.assertIsNone(_duration_validation_error(213.4, row["duration_seconds"]))

        error = _duration_validation_error(213.5, row["duration_seconds"])
        self.assertIsNotNone(error)
        self.assertEqual(error.status_code, 400)
        self.assertIn(b"213.4s", error.body)

    def test_stats_round_aggregated_minutes_to_one_decimal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        """
                        INSERT INTO jobs (
                            id, url, type, status, finished_at, duration_seconds, lalal_split_done
                        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                        """,
                        ("audio-job", "https://example.com/audio", "audio", "done", 213.4, 1),
                    )
                    connection.execute(
                        """
                        INSERT INTO jobs (id, url, type, status, finished_at, duration_seconds)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                        """,
                        ("video-job", "https://example.com/video", "video", "done", 213.4),
                    )
                    connection.commit()

                    stored_type = connection.execute(
                        "SELECT typeof(duration_seconds) FROM jobs WHERE id='audio-job'"
                    ).fetchone()[0]

                stats = db.get_stats()

        self.assertEqual(stored_type, "real")
        self.assertEqual(stats["total_minutes"], 7.1)
        self.assertEqual(stats["total_lalal_minutes"], 3.6)


if __name__ == "__main__":
    unittest.main()
