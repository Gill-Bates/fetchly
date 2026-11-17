#!/usr/bin/env python3
#
# tests/test_bpm_normalization.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import math
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audio import beat_this, bpm
from app.audio.beat_this import BeatThisResult
from app.audio.bpm import BPMResult
from app.audio.normalization import normalize_bpm


class NormalizeBpmTests(unittest.TestCase):
    def test_rejects_non_positive_and_non_finite_values(self) -> None:
        for value in (0.0, -1.0, math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                self.assertEqual(normalize_bpm(value), 0.0)

    def test_preserves_values_inside_supported_range(self) -> None:
        for value in (70.0, 120.0, 180.0):
            with self.subTest(value=value):
                self.assertEqual(normalize_bpm(value), value)

    def test_normalizes_binary_tempo_multiples(self) -> None:
        cases = {
            35.0: 70.0,
            45.0: 90.0,
            181.0: 90.5,
            360.0: 180.0,
            720.0: 180.0,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_bpm(value), expected)


class BpmCascadeValidationTests(unittest.TestCase):
    def test_non_finite_essentia_confidence_cannot_boost_the_fused_result(self) -> None:
        for confidence in (math.nan, math.inf):
            with self.subTest(confidence=confidence):
                with (
                    patch.object(bpm, "_ensure_file_exists"),
                    patch.object(bpm, "_run_ffmpeg"),
                    patch.object(bpm, "_extract_bpm_essentia", return_value=BPMResult(120.0, confidence)),
                ):
                    direct_result = bpm.extract_bpm(Path("audio.wav"))
                self.assertEqual(direct_result, BPMResult(0.0, 0.0))

                with (
                    patch.object(bpm, "_ensure_file_exists"),
                    patch.object(bpm, "extract_bpm", return_value=BPMResult(120.0, confidence)),
                    patch.object(bpm, "_run_ffmpeg"),
                    patch.object(beat_this, "is_beat_this_available", return_value=True),
                    patch.object(
                        beat_this,
                        "extract_bpm_beat_this",
                        return_value=BeatThisResult(121.0, 0.9, 16),
                    ),
                ):
                    result = bpm.extract_bpm_cascade(Path("audio.wav"))

                self.assertEqual(result.bpm, 121.0)
                self.assertEqual(result.confidence, 0.9)

    def test_beat_this_zero_confidence_is_not_an_accepted_tempo(self) -> None:
        with (
            patch.object(bpm, "_ensure_file_exists"),
            patch.object(bpm, "extract_bpm", return_value=BPMResult(0.0, 0.0)),
            patch.object(bpm, "_run_ffmpeg"),
            patch.object(beat_this, "is_beat_this_available", return_value=True),
            patch.object(
                beat_this,
                "extract_bpm_beat_this",
                return_value=BeatThisResult(120.0, 0.0, 16),
            ),
        ):
            result = bpm.extract_bpm_cascade(Path("audio.wav"))

        self.assertEqual(result, BPMResult(0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
