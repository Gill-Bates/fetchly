#!/usr/bin/env python3
#
# tests/test_lalal_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest

from app.integrations.lalal.policy import (
    LALAL_MAX_DURATION_MINUTES,
    LALAL_MAX_DURATION_SECONDS,
    LALAL_MINUTES_UNKNOWN,
    minutes_from_setting,
    minutes_to_setting,
)


class LalalPolicyTests(unittest.TestCase):
    def test_minute_label_is_derived_from_seconds_limit(self) -> None:
        self.assertEqual(LALAL_MAX_DURATION_SECONDS, 600)
        self.assertEqual(LALAL_MAX_DURATION_MINUTES, LALAL_MAX_DURATION_SECONDS // 60)

    def test_zero_minutes_is_a_real_balance_not_unknown(self) -> None:
        for value in (0, 0.0, "0"):
            with self.subTest(value=value):
                self.assertEqual(minutes_from_setting(value), 0.0)
        self.assertEqual(minutes_from_setting(minutes_to_setting(0.0)), 0.0)

    def test_unknown_sentinel_and_missing_values_stay_unknown(self) -> None:
        for value in (None, LALAL_MINUTES_UNKNOWN, -1.0, -0.5):
            with self.subTest(value=value):
                self.assertIsNone(minutes_from_setting(value))
        self.assertEqual(minutes_to_setting(None), LALAL_MINUTES_UNKNOWN)
        self.assertEqual(minutes_from_setting(12.5), 12.5)


if __name__ == "__main__":
    unittest.main()
