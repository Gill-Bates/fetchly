#!/usr/bin/env python3
#
# tests/test_cookie_routes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""End-to-end cover for the Settings cookie box.

Walks what the paste dialog actually does: send what the dev tools produced,
get a jar on disk that yt-dlp can read, and get told off for the one paste
that looks right but carries no login.
"""

import json
import os
import stat
from pathlib import Path
from time import time
from unittest.mock import patch

from app.utils import cookies
from tests._support import WebAppTestCase

SIGNED_IN_HEADER = (
    "VISITOR_INFO1_LIVE=abc; __Secure-3PSID=g.a000xyz; __Secure-3PAPISID=aG5YCo; "
    "LOGIN_INFO=AFmmF2s:QUQ3MjN"
)


class CookieRouteTests(WebAppTestCase):
    def setUp(self):
        super().setUp()

        self.cookie_dir = Path(self._tmp.name) / "cookies"
        self.cookie_dir.mkdir()
        dir_patcher = patch.object(cookies, "_DATA_COOKIES_DIR", self.cookie_dir)
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)

    def _paste(self, platform: str, text: str):
        self.client.get("/login")
        token = self.client.cookies.get("fetchly_csrf")
        return self.client.post(
            f"/api/cookies/{platform}/paste",
            json={"text": text},
            headers={"X-CSRF-Token": token},
        )

    def _stored(self, platform: str = "youtube") -> Path:
        return self.cookie_dir / f"{platform}_cookies.txt"

    def test_a_copied_header_lands_as_a_netscape_jar(self):
        response = self._paste("youtube", SIGNED_IN_HEADER)
        self.assertEqual(response.status_code, 200, response.text)

        payload = response.json()
        self.assertEqual(payload["status"], "valid")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["filename"], "youtube_cookies.txt")
        # A copied header carries no expiry, so none is reported.
        self.assertIsNone(payload["expires_at"])

        stored = self._stored()
        self.assertTrue(stored.is_file())
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID", stored.read_text())
        # The jar holds a live login: owner-only from the moment it is written.
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)

    def test_a_paste_without_a_login_cookie_is_refused(self):
        # What document.cookie in the console returns - the HttpOnly login
        # cookies are missing, and storing it would leave downloads signed out
        # while Settings claimed cookies were in place.
        response = self._paste("youtube", "VISITOR_INFO1_LIVE=abc; PREF=f6=40000000")

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("document.cookie", detail)
        self.assertIn("LOGIN_INFO", detail)
        self.assertFalse(self._stored().exists())

    def test_unreadable_input_is_refused(self):
        response = self._paste("youtube", "I have no idea what I am doing")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self._stored().exists())

    def test_unknown_platform_is_a_404(self):
        self.assertEqual(self._paste("vimeo", SIGNED_IN_HEADER).status_code, 404)

    def test_an_expired_jar_is_reported_as_expired(self):
        past = int(time()) - 86_400
        self._stored().write_text(
            "# Netscape HTTP Cookie File\n"
            f".youtube.com\tTRUE\t/\tTRUE\t{past}\t__Secure-3PSID\tvalue\n",
            encoding="utf-8",
        )

        statuses = {entry["platform"]: entry for entry in self.client.get("/api/cookies").json()}
        self.assertEqual(statuses["youtube"]["status"], "expired")
        self.assertEqual(statuses["youtube"]["expires_at"], past)
        # Every platform keeps its tile, with or without a file.
        self.assertEqual(statuses["tiktok"]["status"], "missing")

    def test_publishing_repairs_permissions_of_an_existing_jar(self):
        # O_CREAT's mode only applies when the file did not exist, so the old
        # overwrite-in-place left a world-readable jar world-readable.
        stored = self._stored()
        stored.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        os.chmod(stored, 0o644)

        self.assertEqual(self._paste("youtube", SIGNED_IN_HEADER).status_code, 200)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)

    def test_no_scratch_files_are_left_in_the_cookie_directory(self):
        self.assertEqual(self._paste("youtube", SIGNED_IN_HEADER).status_code, 200)
        self.assertEqual(self._paste("youtube", "nonsense").status_code, 400)

        leftovers = [p.name for p in self.cookie_dir.iterdir() if p.name != "youtube_cookies.txt"]
        self.assertEqual(leftovers, [])

    def test_an_oversized_paste_is_refused_by_byte_length(self):
        # 100k emoji are only 100k characters but 400k bytes - a character
        # limit alone lets a paste through at several times its intended size.
        oversized = "\U0001F36A" * 100_000
        self.assertLess(len(oversized), 256 * 1024)
        self.assertGreater(len(oversized.encode("utf-8")), 256 * 1024)

        self.assertEqual(self._paste("youtube", oversized).status_code, 413)
        self.assertFalse(self._stored().exists())

    def test_a_foreign_session_cookie_is_never_stored(self):
        payload = json.dumps(
            [
                {"domain": ".example.com", "name": "sessionid", "value": "foreign"},
                {"domain": ".instagram.com", "name": "sessionid", "value": "own"},
            ]
        )
        self.assertEqual(self._paste("instagram", payload).status_code, 200)

        stored = self._stored("instagram").read_text(encoding="utf-8")
        self.assertIn("own", stored)
        self.assertNotIn("foreign", stored)
        self.assertNotIn("example.com", stored)

    def test_a_pasted_jar_can_be_removed_again(self):
        self.assertEqual(self._paste("youtube", SIGNED_IN_HEADER).status_code, 200)

        self.client.get("/login")
        response = self.client.request(
            "DELETE",
            "/api/cookies/youtube",
            headers={"X-CSRF-Token": self.client.cookies.get("fetchly_csrf")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self._stored().exists())
