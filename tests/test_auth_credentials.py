#!/usr/bin/env python3
#
# tests/test_auth_credentials.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The database-backed admin account.

fetchly ships with authentication off and no credentials; there is no
environment variable that can inject an account. These tests pin the fresh
install state, the fail-closed behaviour when the account is missing, and the
salt-follows-username property of the stored hash.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-auth-credentials-secret")

from app import db
from app import session
from app.routes import auth
from app.utils.credentials import normalize_admin_username, validate_admin_password


class _IsolatedSettingsDb(unittest.TestCase):
    """Point db.DB_PATH at a throwaway database for the duration of one test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        patcher = patch.object(db, "DB_PATH", Path(self._tmp.name) / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db._database_path_prepared = None
        self.addCleanup(setattr, db, "_database_path_prepared", None)
        self.addCleanup(db.close_db)
        db.init_db()

        auth.init_auth(object(), "test-auth-credentials-secret")

    def _store_account(self, username: str, password: str) -> None:
        db.set_settings(
            {
                "admin_username": username,
                "admin_password_hash": auth.hash_password(username, password),
            },
            allow_internal=True,
        )


class FreshInstallTests(_IsolatedSettingsDb):
    def test_authentication_is_off_and_no_account_exists(self):
        settings = db.get_settings(include_internal=True)
        self.assertFalse(settings["enable_authentication"])
        self.assertEqual(settings["admin_username"], "")
        self.assertEqual(settings.get("admin_password_hash", ""), "")
        self.assertFalse(auth.has_admin_credentials())
        self.assertFalse(auth.is_authentication_enabled())

    def test_login_fails_closed_without_an_account(self):
        # Even with the flag forced on, an empty account must not authenticate.
        db.set_settings({"enable_authentication": True})
        with self.assertLogs(auth.logger, level="ERROR"):
            self.assertFalse(auth.verify_login("admin", "whatever"))


class VerifyLoginTests(_IsolatedSettingsDb):
    def setUp(self):
        super().setUp()
        self._store_account("alice", "correct-horse")
        db.set_settings({"enable_authentication": True})

    def test_correct_credentials_accepted(self):
        self.assertTrue(auth.verify_login("alice", "correct-horse"))

    def test_wrong_password_rejected(self):
        self.assertFalse(auth.verify_login("alice", "correct-horser"))

    def test_wrong_username_rejected(self):
        self.assertFalse(auth.verify_login("bob", "correct-horse"))

    def test_non_ascii_username_is_rejected_not_raised(self):
        # hmac.compare_digest() refuses non-ASCII str operands; comparing the
        # UTF-8 bytes keeps a junk username a failed login instead of a 500.
        self.assertFalse(auth.verify_login("bäcker", "correct-horse"))
        self.assertFalse(auth.verify_login("アリス", "correct-horse"))

    def test_hash_is_salted_per_username(self):
        # The PBKDF2 salt is derived from the username, so the same password
        # under a different name must not produce the same hash - otherwise a
        # rename would silently keep the old credential valid.
        self.assertNotEqual(
            auth.hash_password("alice", "correct-horse"),
            auth.hash_password("bob", "correct-horse"),
        )

    def test_rename_requires_rehash(self):
        self._store_account("bob", "correct-horse")
        self.assertTrue(auth.verify_login("bob", "correct-horse"))
        self.assertFalse(auth.verify_login("alice", "correct-horse"))


class LocalUserTests(_IsolatedSettingsDb):
    def test_local_principal_while_authentication_is_off(self):
        request = type("R", (), {"cookies": {}})()
        self.assertEqual(auth.current_user(request), auth.LOCAL_USER)

    def test_no_session_means_no_user_once_enabled(self):
        self._store_account("alice", "correct-horse")
        db.set_settings({"enable_authentication": True})
        request = type("R", (), {"cookies": {}})()
        self.assertIsNone(auth.current_user(request))


class SecureCookieResolutionTests(unittest.TestCase):
    def test_https_proxy_override_marks_cookies_secure_for_http_upstreams(self):
        request = type("R", (), {"url": type("U", (), {"scheme": "http"})()})()
        with patch.dict(os.environ, {"FETCHLY_BEHIND_HTTPS": "1"}):
            self.assertTrue(session._resolve_cookie_secure(request))

    def test_plain_http_without_proxy_override_keeps_cookies_non_secure(self):
        request = type("R", (), {"url": type("U", (), {"scheme": "http"})()})()
        with patch.dict(os.environ, {"FETCHLY_BEHIND_HTTPS": ""}):
            self.assertFalse(session._resolve_cookie_secure(request))


class CredentialValidationTests(unittest.TestCase):
    def test_username_rules(self):
        self.assertEqual(normalize_admin_username("  admin "), "admin")
        self.assertEqual(normalize_admin_username(""), "")
        self.assertEqual(normalize_admin_username(None), "")
        for bad in (
            "has space",
            "colon:name",
            "a" * 65,
            "sla/sh",
            "bäcker",
            "digit1",
            "dot.name",
            "at@name",
        ):
            with self.assertRaises(ValueError):
                normalize_admin_username(bad)

    def test_username_never_contains_the_session_delimiter(self):
        # app/session.py packs the username into a colon-delimited token.
        with self.assertRaises(ValueError):
            normalize_admin_username("user:name")

    def test_password_rules(self):
        self.assertEqual(validate_admin_password("12345678"), "12345678")
        # Surrounding whitespace is part of the password, not noise to trim.
        self.assertEqual(validate_admin_password("  spaced  "), "  spaced  ")
        for bad in ("", "short", "x" * 1025):
            with self.assertRaises(ValueError):
                validate_admin_password(bad)


if __name__ == "__main__":
    unittest.main()
