#!/usr/bin/env python3

import unittest

from app.utils.template_filters import filesize


class FileSizeFilterTests(unittest.TestCase):
    def test_gib_values_use_one_decimal_place(self) -> None:
        gib = 1_073_741_824

        self.assertEqual(str(filesize(gib * 1_567 // 10)), "156.7 GiB")
        self.assertEqual(str(filesize(gib * 1_568 // 10)), "156.8 GiB")
