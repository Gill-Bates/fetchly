#!/usr/bin/env python3
#
# tests/test_bpm_naming.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest
from pathlib import Path

from app.bpm_naming import apply_bpm_tag, tagged_download_name
from app.lalal_policy import stem_download_name


class ApplyBpmTagTests(unittest.TestCase):
    def test_tag_sits_between_title_and_source_marker(self) -> None:
        self.assertEqual(
            apply_bpm_tag("Some Track.source", 94),
            "Some Track_94bpm.source",
        )

    def test_name_without_the_source_marker_gets_the_tag_appended(self) -> None:
        self.assertEqual(apply_bpm_tag("Some Track", 94), "Some Track_94bpm")

    def test_fractional_bpm_is_rounded_to_a_whole_number(self) -> None:
        self.assertEqual(apply_bpm_tag("Some Track", 93.6), "Some Track_94bpm")

    def test_missing_or_unusable_bpm_leaves_the_name_untouched(self) -> None:
        for bpm in (None, 0, -1, "94", True):
            with self.subTest(bpm=bpm):
                self.assertEqual(apply_bpm_tag("Some Track", bpm), "Some Track")


class TaggedDownloadNameTests(unittest.TestCase):
    def test_transcoded_mp3_keeps_its_extension(self) -> None:
        self.assertEqual(
            tagged_download_name(Path("/data/job/Some Track.mp3"), 94),
            "Some Track_94bpm.mp3",
        )

    def test_video_without_a_bpm_is_served_under_its_own_name(self) -> None:
        self.assertEqual(
            tagged_download_name(Path("/data/job/Some Clip (maxQuality).mp4"), None),
            "Some Clip (maxQuality).mp4",
        )


class StemDownloadNameTests(unittest.TestCase):
    def test_stem_suffix_stays_at_the_end_of_the_name(self) -> None:
        self.assertEqual(
            stem_download_name("Some Track.source", "vocals", 94),
            "Some Track_94bpm.source_vocals.mp3",
        )
        self.assertEqual(
            stem_download_name("Some Track.source", "instrumental", 128),
            "Some Track_128bpm.source_instrumental.mp3",
        )

    def test_trim_stem_gets_the_tag_appended(self) -> None:
        self.assertEqual(
            stem_download_name("trim_0_30000", "vocals", 94),
            "trim_0_30000_94bpm_vocals.mp3",
        )

    def test_missing_bpm_leaves_the_stem_name_untagged(self) -> None:
        self.assertEqual(
            stem_download_name("Some Track.source", "vocals", None),
            "Some Track.source_vocals.mp3",
        )


if __name__ == "__main__":
    unittest.main()
