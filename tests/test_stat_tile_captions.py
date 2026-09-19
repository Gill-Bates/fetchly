#!/usr/bin/env python3
#
# tests/test_stat_tile_captions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The phone caption on the stat tiles.

Below 768px the tiles sit four to a row, which leaves no space for the full
label under the value. The first line therefore carries a short word instead of
the icon, because a glyph alone does not say which number it is - "246.5 MiB"
under a download arrow and "165.3 GiB" under a drive both read as "some amount
of storage".

Two things have to hold for that to work, and neither is visible in a diff of
one file: every tile has to *have* a caption (a missing macro argument renders
an empty span, not an error), and the swap has to stay a swap - if the icon
survives next to the word, the tile gains a line and the row grows taller.
"""

import re
import unittest
from pathlib import Path

from tests._support import WebAppTestCase

STAT_CARD = re.compile(r'class="[^"]*\bstat-card\b')
SHORT_LABEL = re.compile(r'<span class="stat-short-label" aria-hidden="true">([^<]*)</span>')
PHONE_MEDIA_QUERY = "@media (max-width: 767.98px) {"


def _media_blocks(css: str, query: str) -> list[str]:
    """Every block introduced by ``query``, brace-matched to its own close."""
    blocks = []
    position = css.find(query)
    while position != -1:
        cursor = position + len(query)
        depth = 1
        while depth and cursor < len(css):
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
            cursor += 1
        blocks.append(css[position:cursor])
        position = css.find(query, cursor)
    return blocks


class StatTileCaptionRenderTests(WebAppTestCase):
    def _assert_every_tile_is_captioned(self, path: str) -> None:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, f"{path} did not render")

        tiles = STAT_CARD.findall(response.text)
        captions = SHORT_LABEL.findall(response.text)
        self.assertGreater(len(tiles), 0, f"{path} rendered no stat tiles")
        self.assertEqual(
            len(captions),
            len(tiles),
            f"{path}: {len(tiles)} stat tiles but {len(captions)} short labels",
        )
        for caption in captions:
            self.assertTrue(caption.strip(), f"{path} rendered an empty stat caption")

    def test_dashboard_tiles_carry_a_short_label(self) -> None:
        self._assert_every_tile_is_captioned("/")

    def test_settings_system_tiles_carry_a_short_label(self) -> None:
        # Both rows: the job tiles moved here from the dashboard, plus the four
        # host metrics that are written out longhand rather than via the macro.
        self._assert_every_tile_is_captioned("/settings")


class StatTileCaptionStyleTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        stylesheet = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
        self.css = stylesheet.read_text()
        # There is more than one phone block in the file, and the rules could
        # equally have landed in the 1024px one - where they would apply to the
        # tablet layout that still has room for the full label. So match the
        # query and walk its braces instead of asserting against the whole file.
        self.phone_css = "\n".join(_media_blocks(self.css, PHONE_MEDIA_QUERY))
        self.assertIn(".stats-row", self.phone_css, "no phone block found")

    def test_short_label_is_out_of_the_layout_by_default(self) -> None:
        self.assertIn(".stat-short-label {\n    display: none;\n}", self.css)

    def test_phone_tiles_swap_the_icon_for_the_word(self) -> None:
        self.assertIn(
            "    .stats-row .stat-icon {\n        display: none;\n    }",
            self.phone_css,
            "the icon has to give up its line, or the tile gains height",
        )
        self.assertIn("    .stats-row .stat-short-label {\n        display: block;", self.phone_css)
