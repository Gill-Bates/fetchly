#!/usr/bin/env python3

import unittest
from pathlib import Path


class MobileSettingsLayoutTests(unittest.TestCase):
    def test_settings_main_keeps_space_before_the_footer_on_mobile(self) -> None:
        stylesheet = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"

        self.assertIn(
            "    .app-root--settings .app-main {\n"
            "        padding-bottom: var(--space-6);\n"
            "    }",
            stylesheet.read_text(),
        )
