#!/usr/bin/env python3
#
# tests/test_cookie_status.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import tempfile
import unittest
from pathlib import Path
from time import time
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-cookie-status-secret")

from app.utils import cookies
from app.utils.cookie_status import analyze_cookie_file, cookie_file_is_usable

FUTURE = int(time()) + 86_400 * 30
PAST = int(time()) - 86_400


def jar(*rows: str) -> str:
    return "# Netscape HTTP Cookie File\n" + "".join(f"{row}\n" for row in rows)


def row(name: str, *, domain: str = ".youtube.com", expires: int = 0) -> str:
    return "\t".join((domain, "TRUE", "/", "TRUE", str(expires), name, "value"))


def signed_in(*, expires: int = 0) -> str:
    """A jar carrying what yt-dlp actually checks for on YouTube."""
    return jar(row("LOGIN_INFO", expires=expires), row("__Secure-3PAPISID", expires=expires))


class AnalyzeCookieFileTests(unittest.TestCase):
    def analyze(self, text: str, platform: str = "youtube"):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.txt"
            path.write_text(text, encoding="utf-8")
            return analyze_cookie_file(path, platform)

    def test_structural_validity_and_authentication_are_separate(self) -> None:
        consent_only = self.analyze(jar(row("PREF"), row("VISITOR_INFO1_LIVE")))
        self.assertEqual(consent_only.status, "valid")
        self.assertTrue(consent_only.is_usable)
        self.assertFalse(consent_only.is_authenticated)
        self.assertEqual(
            consent_only.missing_login_cookies, ("LOGIN_INFO", "SAPISID")
        )

        complete = self.analyze(jar(row("LOGIN_INFO"), row("__Secure-3PAPISID")))
        self.assertTrue(complete.is_authenticated)
        self.assertEqual(complete.missing_login_cookies, ())

    def test_zero_expiry_is_a_session_cookie_not_an_expired_one(self) -> None:
        # yt-dlp writes 0 for session cookies and reads it back as "no expiry";
        # taking the field literally would date every one of them to 1970 and
        # report a working jar as expired.
        analysis = self.analyze(jar(row("__Secure-3PSID")))
        self.assertEqual(analysis.status, "valid")
        self.assertTrue(analysis.is_usable)
        self.assertIsNone(analysis.expires_at)

    def test_a_dated_cookie_reports_its_expiry(self) -> None:
        analysis = self.analyze(jar(row("SID", expires=FUTURE), row("PREF", expires=FUTURE + 99)))
        self.assertEqual(analysis.status, "valid")
        # The soonest expiry decides how long the jar is good for.
        self.assertEqual(analysis.expires_at, FUTURE)

    def test_expired_cookies_are_reported_and_not_usable(self) -> None:
        analysis = self.analyze(jar(row("SID", expires=PAST)))
        self.assertEqual(analysis.status, "expired")
        self.assertFalse(analysis.is_usable)
        self.assertEqual(analysis.expires_at, PAST)

    def test_one_live_cookie_keeps_the_jar_usable(self) -> None:
        analysis = self.analyze(jar(row("SID", expires=PAST), row("__Secure-3PSID", expires=FUTURE)))
        self.assertEqual(analysis.status, "valid")
        self.assertEqual(analysis.expires_at, FUTURE)

    def test_another_platforms_cookies_are_invalid(self) -> None:
        analysis = self.analyze(jar(row("SID", domain=".tiktok.com", expires=FUTURE)))
        self.assertEqual(analysis.status, "invalid")
        self.assertFalse(analysis.is_usable)

    def test_google_cookies_count_for_youtube(self) -> None:
        analysis = self.analyze(jar(row("SID", domain=".google.com", expires=FUTURE)))
        self.assertEqual(analysis.status, "valid")

    def test_garbage_and_missing_files(self) -> None:
        self.assertEqual(self.analyze("not a cookie file at all\n").status, "invalid")
        self.assertEqual(self.analyze(jar()).status, "invalid")

        with tempfile.TemporaryDirectory() as temp_dir:
            absent = Path(temp_dir) / "nope.txt"
            analysis = analyze_cookie_file(absent, "youtube")
            self.assertEqual(analysis.status, "missing")
            self.assertFalse(analysis.present)
            self.assertFalse(cookie_file_is_usable(absent, "youtube"))


class WorkerCookieGateTests(unittest.TestCase):
    """The download path must ignore a jar it would only get blocked with."""

    def run_with_jar(self, text: str) -> list[str]:
        from app import worker

        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_dir = Path(temp_dir) / "cookies"
            cookie_dir.mkdir()
            (cookie_dir / "youtube_cookies.txt").write_text(text, encoding="utf-8")
            with patch.object(cookies, "_DATA_COOKIES_DIR", cookie_dir):
                return worker._cookies_args_for_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_a_valid_jar_is_passed_to_yt_dlp(self) -> None:
        args = self.run_with_jar(signed_in(expires=FUTURE))
        self.assertEqual(args[0], "--cookies")
        self.assertTrue(args[1].endswith("youtube_cookies.txt"))

    def test_an_expired_jar_is_left_out(self) -> None:
        self.assertEqual(self.run_with_jar(signed_in(expires=PAST)), [])

    def test_a_jar_without_login_cookies_is_left_out(self) -> None:
        # Structurally fine - real youtube.com cookies, unexpired - but it
        # authenticates nothing, so sending it only marks the request as a
        # stale login. The Settings tile promises this fallback.
        consent_only = jar(row("PREF", expires=FUTURE), row("VISITOR_INFO1_LIVE", expires=FUTURE))
        self.assertEqual(self.run_with_jar(consent_only), [])

    def test_an_unreadable_jar_is_left_out(self) -> None:
        self.assertEqual(self.run_with_jar("this is not a cookie file\n"), [])


if __name__ == "__main__":
    unittest.main()
