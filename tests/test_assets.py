#!/usr/bin/env python3
#
# tests/test_assets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cache busting for /static.

The tokens in the templates used to be typed by hand, and the failure mode was
silent: a commit changes style.css, forgets to bump `?v=`, and every browser
that already has the file keeps it. New markup, old stylesheet. That is what
made the stat-tile caption render in the number's own font on a phone while
the same page was correct in a fresh browser.

What has to hold now is that the token is *derived*: it changes with the file,
the page points at a URL that really exists, and the cache policy only promises
immutability for a token that still matches the bytes on disk.
"""

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils import assets
from tests._support import WebAppTestCase

VERSIONED_STATIC = re.compile(r'["\'](/static/[^"\'?\s]+)\?v=([0-9a-f]+)["\']')
HANDWRITTEN_TOKEN = re.compile(r'["\'](/static/[^"\'?\s]+)\?v=(?![0-9a-f]{8}["\'])')


class AssetUrlTests(unittest.TestCase):
    def test_appends_content_hash(self) -> None:
        url = assets.asset_url("/static/style.css")

        path, _, query = url.partition("?")
        self.assertEqual(path, "/static/style.css")
        digest = hashlib.sha256(Path("app/static/style.css").read_bytes()).hexdigest()
        self.assertEqual(query, f"v={digest[:8]}")

    def test_token_follows_the_file(self) -> None:
        """The point of the whole mechanism: edit the file, get a new URL."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.css").write_text("a{}")
            with patch.object(assets, "_STATIC_ROOT", root):
                before = assets.asset_url("/static/app.css")
                # Same content, different bytes: a second write alone must not
                # be enough, or the hash would just be a timestamp.
                (root / "app.css").write_text("a{}")
                unchanged = assets.asset_url("/static/app.css")
                (root / "app.css").write_text("a{color:red}")
                after = assets.asset_url("/static/app.css")

        self.assertEqual(before, unchanged)
        self.assertNotEqual(before, after)

    def test_unreadable_asset_stays_unversioned(self) -> None:
        """A missing file is a 404 for the browser, not a broken page."""
        self.assertEqual(assets.asset_url("/static/nope.css"), "/static/nope.css")

    def test_rejects_paths_outside_static(self) -> None:
        self.assertEqual(assets.asset_url("/static/../main.py"), "/static/../main.py")
        self.assertEqual(assets.asset_url("/etc/passwd"), "/etc/passwd")


class RenderedAssetReferenceTests(WebAppTestCase):
    def _assert_page_assets_are_versioned(self, path: str) -> None:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, f"{path} did not render")

        self.assertIsNone(
            HANDWRITTEN_TOKEN.search(response.text),
            f"{path} carries a hand-written ?v= token; use asset_url()",
        )

        references = VERSIONED_STATIC.findall(response.text)
        self.assertGreater(len(references), 0, f"{path} references no versioned asset")
        for asset_path, token in references:
            self.assertEqual(
                token,
                assets.asset_version(asset_path),
                f"{path} points at a stale token for {asset_path}",
            )
            served = self.client.get(f"{asset_path}?v={token}")
            self.assertEqual(served.status_code, 200, f"{asset_path} is not served")

    def test_dashboard_assets_are_versioned(self) -> None:
        self._assert_page_assets_are_versioned("/")

    def test_settings_assets_are_versioned(self) -> None:
        self._assert_page_assets_are_versioned("/settings")

    def test_stylesheet_matches_the_file_on_disk(self) -> None:
        """The regression itself: the page's CSS URL must address this CSS."""
        response = self.client.get("/")
        token = re.search(r'/static/style\.css\?v=([0-9a-f]+)', response.text)
        self.assertIsNotNone(token, "the dashboard does not link style.css")

        served = self.client.get(f"/static/style.css?v={token.group(1)}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.text, Path("app/static/style.css").read_text())


class StaticCacheControlTests(WebAppTestCase):
    def test_matching_token_is_immutable(self) -> None:
        version = assets.asset_version("/static/style.css")
        response = self.client.get(f"/static/style.css?v={version}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], assets.IMMUTABLE_CACHE_CONTROL)

    def test_stale_token_revalidates(self) -> None:
        """An old page in a cache must not be able to pin an old asset."""
        response = self.client.get("/static/style.css?v=deadbeef")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], assets.REVALIDATE_CACHE_CONTROL)

    def test_unversioned_asset_revalidates(self) -> None:
        """Covers the module imports, which carry no token of their own."""
        response = self.client.get("/static/js/config.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], assets.REVALIDATE_CACHE_CONTROL)


if __name__ == "__main__":
    unittest.main()
