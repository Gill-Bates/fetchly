#!/usr/bin/env python3
#
# tests/test_bpm_beat_this.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cover for the tempo maths in app/bpm_beat_this.py.

These exercise the part that turns beat timestamps into a BPM. They are
deliberately independent of the model: the checkpoint is a download away and
nothing here needs it.
"""

import unittest

import numpy as np

from app.bpm_beat_this import _MIN_BEATS_FOR_BPM, _bpm_from_beats


def beats_at(bpm: float, count: int, jitter: np.ndarray | None = None) -> np.ndarray:
    interval = 60.0 / bpm
    times = np.arange(count, dtype=float) * interval
    return times if jitter is None else times + jitter


class BpmFromBeatsTests(unittest.TestCase):
    def test_a_steady_pulse_gives_its_tempo(self) -> None:
        bpm, confidence = _bpm_from_beats(beats_at(120.0, 32))
        self.assertAlmostEqual(bpm, 120.0, places=6)
        self.assertAlmostEqual(confidence, 1.0, places=6)

    def test_the_median_absorbs_a_dropped_beat(self) -> None:
        # One missing beat doubles a single interval. This is the robustness
        # that made the DBN's smoothing redundant for a median-based tempo.
        beats = np.delete(beats_at(100.0, 33), 7)
        bpm, _ = _bpm_from_beats(beats)
        self.assertAlmostEqual(bpm, 100.0, places=6)

    def test_jitter_lowers_confidence_without_moving_the_tempo(self) -> None:
        rng = np.random.default_rng(1)
        beats = beats_at(90.0, 64, jitter=rng.normal(0, 0.02, 64))
        bpm, confidence = _bpm_from_beats(beats)
        self.assertAlmostEqual(bpm, 90.0, delta=1.5)
        self.assertLess(confidence, 1.0)
        self.assertGreater(confidence, 0.5)

    def test_too_few_beats_and_degenerate_input_report_nothing(self) -> None:
        self.assertEqual(_bpm_from_beats(beats_at(120.0, _MIN_BEATS_FOR_BPM - 1)), (0.0, 0.0))
        self.assertEqual(_bpm_from_beats(np.zeros(8)), (0.0, 0.0))

    def test_confidence_never_leaves_the_unit_range(self) -> None:
        chaotic = np.cumsum(np.array([0.1, 3.0, 0.1, 2.5, 0.1, 4.0, 0.2, 3.5]))
        _, confidence = _bpm_from_beats(chaotic)
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 1.0)


if __name__ == "__main__":
    unittest.main()
