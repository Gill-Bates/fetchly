#!/usr/bin/env python3
#
# tests/test_changelog.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest
from unittest.mock import patch

from app.utils import changelog


class RenderChangelogTests(unittest.TestCase):
    def setUp(self):
        changelog.render_changelog_html.cache_clear()

    def tearDown(self):
        changelog.render_changelog_html.cache_clear()

    def test_repo_changelog_renders_to_structural_html(self):
        html = changelog.get_changelog_html()
        self.assertIn("<h2>", html)
        self.assertIn("<li>", html)
        self.assertIn("<code>", html)
        # The "Previous versions" disclosure survives sanitisation.
        self.assertIn("<details>", html)
        self.assertIn("<summary>", html)

    def test_dangerous_markup_is_stripped(self):
        malicious = (
            "## [9.9.9] - 2099-01-01\n\n"
            "- <script>alert(1)</script> bad\n"
            "- <img src=x onerror=alert(1)> worse\n"
            '- <a href="javascript:alert(1)">link</a>\n'
        )
        with patch.object(changelog, "_CHANGELOG_PATH") as fake_path:
            fake_path.read_text.return_value = malicious
            html = changelog.render_changelog_html()

        self.assertNotIn("<script", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("javascript:", html)
        self.assertIn("<h2>", html)

    def test_missing_file_returns_placeholder(self):
        with patch.object(changelog, "_CHANGELOG_PATH") as fake_path:
            fake_path.read_text.side_effect = FileNotFoundError
            self.assertEqual(
                changelog.render_changelog_html(), "<p>Changelog not found.</p>"
            )

    def test_render_failure_is_swallowed(self):
        with patch.object(changelog, "render_changelog_html", side_effect=RuntimeError("boom")):
            with self.assertLogs(changelog.logger, level="WARNING"):
                result = changelog.get_changelog_html()
        self.assertEqual(result, "<p>Changelog could not be rendered.</p>")


if __name__ == "__main__":
    unittest.main()
