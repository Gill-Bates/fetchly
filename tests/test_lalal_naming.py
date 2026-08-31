#!/usr/bin/env python3
#
# tests/test_lalal_naming.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest

from app.lalal_policy import stem_download_name


class StemDownloadNameTests(unittest.TestCase):
    def test_bpm_tag_sits_between_title_and_source_marker(self) -> None:
        self.assertEqual(
            stem_download_name("Some Track.source", "vocals", 94),
            "Some Track_94bpm.source_vocals.mp3",
        )
        self.assertEqual(
            stem_download_name("Some Track.source", "instrumental", 128),
            "Some Track_128bpm.source_instrumental.mp3",
        )

    def test_names_without_the_source_marker_get_the_tag_appended(self) -> None:
        self.assertEqual(
            stem_download_name("trim_0_30000", "vocals", 94),
            "trim_0_30000_94bpm_vocals.mp3",
        )

    def test_fractional_bpm_is_rounded_to_a_whole_number(self) -> None:
        self.assertEqual(
            stem_download_name("Some Track.source", "vocals", 93.6),
            "Some Track_94bpm.source_vocals.mp3",
        )

    def test_missing_or_unusable_bpm_leaves_the_name_untagged(self) -> None:
        for bpm in (None, 0, -1, "94", True):
            with self.subTest(bpm=bpm):
                self.assertEqual(
                    stem_download_name("Some Track.source", "vocals", bpm),
                    "Some Track.source_vocals.mp3",
                )


if __name__ == "__main__":
    unittest.main()
