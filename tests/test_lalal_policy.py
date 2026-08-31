#!/usr/bin/env python3
#
# tests/test_lalal_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest

from app.lalal_policy import LALAL_MAX_DURATION_MINUTES, LALAL_MAX_DURATION_SECONDS


class LalalPolicyTests(unittest.TestCase):
    def test_minute_label_is_derived_from_seconds_limit(self) -> None:
        self.assertEqual(LALAL_MAX_DURATION_SECONDS, 600)
        self.assertEqual(LALAL_MAX_DURATION_MINUTES, LALAL_MAX_DURATION_SECONDS // 60)


if __name__ == "__main__":
    unittest.main()
