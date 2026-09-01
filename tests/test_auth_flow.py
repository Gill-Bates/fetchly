#!/usr/bin/env python3
#
# tests/test_auth_flow.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""End-to-end cover for the enable-authentication flow.

Walks the path a fresh install actually takes: open, refuse to switch the login
on without an account, create the account, get bounced to /login, sign in.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-auth-flow-secret")

from fastapi.testclient import TestClient

from app import db
from app.routes import auth


class AuthFlowTests(unittest.TestCase):
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

        from app.main import app, templates
        from app.routes.api import init_api
        from app.session import refresh_session_settings_cache

        # Both normally happen in the lifespan handler, which TestClient does
        # not run here.
        init_api(templates)
        refresh_session_settings_cache()

        # /api/settings is capped at 5/minute and the limiter is process-wide,
        # so without this the later tests in this class get 429s from the
        # earlier ones.
        from app.common.rate_limit import limiter

        limiter.reset()
        self.addCleanup(limiter.reset)

        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def _sign_in_as(self, username: str) -> None:
        """Attach a valid session cookie without going through the login form.

        The login route is guarded by the hidden CAPTCHA's minimum-dwell check,
        which would make this test sleep; the token minted here is the exact
        one the app would hand out after a successful sign-in.
        """
        from app.session import SESSION_COOKIE, create_session

        self.client.cookies.set(SESSION_COOKIE, create_session(username))

    def _csrf(self) -> str:
        """Prime the CSRF cookie by loading a page, then return its value."""
        self.client.get("/login")
        token = self.client.cookies.get("fetchly_csrf")
        self.assertTrue(token, "CSRF cookie was not issued")
        return token

    def _post_settings(self, body: dict):
        return self.client.post(
            "/api/settings",
            json=body,
            headers={"X-CSRF-Token": self._csrf()},
        )

    def test_fresh_install_needs_no_login(self):
        self.assertFalse(auth.is_authentication_enabled())
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="logoutBtn"', response.text)
        self.assertNotIn('id="logoutBtn"', self.client.get("/settings").text)

    def test_enabling_without_an_account_is_refused(self):
        response = self._post_settings({"enable_authentication": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("username and password", response.json()["detail"])
        # Nothing was persisted.
        self.assertFalse(db.get_settings()["enable_authentication"])

    def test_create_account_enables_auth_and_forces_sign_in(self):
        response = self._post_settings(
            {
                "admin_username": "alice",
                "admin_password": "correct-horse",
                "enable_authentication": True,
            }
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["redirect"], "/login")

        settings = db.get_settings(include_internal=True)
        self.assertTrue(settings["enable_authentication"])
        self.assertEqual(settings["admin_username"], "alice")
        self.assertTrue(settings["admin_password_hash"])

        # The dashboard is now gated.
        self.assertEqual(self.client.get("/").status_code, 303)

        # ...and the new credentials work.
        self.assertTrue(auth.verify_login("alice", "correct-horse"))
        self.assertFalse(auth.verify_login("alice", "wrong"))
        self._sign_in_as("alice")
        self.assertIn('id="logoutBtn"', self.client.get("/").text)

    def test_credentials_survive_disabling_auth(self):
        self._post_settings(
            {
                "admin_username": "alice",
                "admin_password": "correct-horse",
                "enable_authentication": True,
            }
        )
        # Enabling logged the caller out, so changing anything now needs a
        # session again.
        self._sign_in_as("alice")
        self.assertEqual(
            self._post_settings({"enable_authentication": False}).status_code, 200
        )

        settings = db.get_settings(include_internal=True)
        self.assertFalse(settings["enable_authentication"])
        # Kept on purpose: re-enabling must not require inventing a new account.
        self.assertEqual(settings["admin_username"], "alice")
        self.assertTrue(settings["admin_password_hash"])

        # With an account on file the toggle alone is enough to switch back on.
        response = self._post_settings({"enable_authentication": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(db.get_settings()["enable_authentication"])

    def test_password_without_username_is_refused(self):
        response = self._post_settings({"admin_password": "correct-horse"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Username", response.json()["detail"])

    def test_weak_password_is_refused(self):
        response = self._post_settings(
            {"admin_username": "alice", "admin_password": "short"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 8", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
