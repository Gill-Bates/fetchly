#!/usr/bin/env python3
#
# tests/test_bpm_normalization.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import math
import unittest

from app.bpm_normalization import normalize_bpm


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


if __name__ == "__main__":
    unittest.main()
