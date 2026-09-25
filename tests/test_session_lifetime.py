#!/usr/bin/env python3
#
# tests/test_session_lifetime.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cover for the absolute session lifetime (session_max_days).

The setting is the only session deadline: it is counted from login, is never
extended (there is no renewal), and the cookie's Max-Age has to expire with it.
"""

from app import db, session
from tests._support import IsolatedDbTestCase, WebAppTestCase


class SessionLifetimeTests(IsolatedDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        session.refresh_session_settings_cache()
        self.addCleanup(session.refresh_session_settings_cache)

    def _set_days(self, days: int) -> None:
        db.set_settings({"session_max_days": days})
        session.refresh_session_settings_cache()

    def test_default_is_seven_days(self):
        self.assertEqual(db.get_settings()["session_max_days"], 7)
        self.assertEqual(session._get_max_lifetime_seconds(), 7 * 24 * 60 * 60)

    def test_values_outside_one_through_seven_days_are_rejected(self):
        for rejected in (0, 8, 30):
            with self.subTest(days=rejected), self.assertRaises(ValueError):
                db.set_settings({"session_max_days": rejected})
        self.assertEqual(db.get_settings()["session_max_days"], 7)

    def test_lifetime_falls_back_when_the_cached_value_is_unusable(self):
        session._SESSION_SETTINGS_CACHE["session_max_days"] = "not-a-number"
        self.assertEqual(session._get_max_lifetime_seconds(), 7 * 24 * 60 * 60)

    def test_token_is_valid_within_the_configured_lifetime(self):
        self._set_days(7)
        token = session.create_session("alice")
        self.assertEqual(session.validate_session(token), "alice")

    def test_token_is_invalid_once_the_lifetime_elapsed(self):
        self._set_days(1)
        token = session.create_session("alice")
        parsed = session.parse_session(token)
        assert parsed is not None
        just_past = parsed.issued_at + 24 * 60 * 60

        self.assertTrue(session._is_session_expired(parsed, just_past))
        self.assertFalse(session._is_session_expired(parsed, just_past - 1))

    def test_cookie_max_age_expires_with_the_session(self):
        self._set_days(2)
        token = session.create_session("alice")
        parsed = session.parse_session(token)
        assert parsed is not None

        max_age = session._get_cookie_max_age(token)
        self.assertLessEqual(max_age, 2 * 24 * 60 * 60)
        self.assertGreater(max_age, 2 * 24 * 60 * 60 - 5)
        self.assertLessEqual(parsed.issued_at + max_age, parsed.issued_at + 2 * 24 * 60 * 60)


class SessionLifetimeRouteTests(WebAppTestCase):
    """Cover the actual server-side effect: a request with an expired

    session cookie is treated as unauthenticated end to end, through
    require_user()/require_html_auth() - not just at the session.py unit
    level.
    """

    def setUp(self) -> None:
        super().setUp()
        db.set_settings({"enable_authentication": True, "admin_username": "alice"})
        db.set_settings({"admin_password_hash": "irrelevant-for-this-test"}, allow_internal=True)
        session.refresh_session_settings_cache()

    def _cookie_for_age(self, age_seconds: int) -> str:
        """A session token that looks like it was issued ``age_seconds`` ago."""
        token = session.create_session("alice")
        parsed = session.parse_session(token)
        assert parsed is not None
        backdated_issued_at = parsed.issued_at - age_seconds
        payload = f"{parsed.username}:{backdated_issued_at}:{parsed.nonce}:{parsed.session_version}"
        return session._encode_token(payload, session._sign_payload(payload))

    def test_html_page_redirects_to_login_once_the_session_expired(self):
        db.set_settings({"session_max_days": 1})
        session.refresh_session_settings_cache()

        self.client.cookies.set(session.SESSION_COOKIE, self._cookie_for_age(24 * 60 * 60))
        response = self.client.get("/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_html_page_stays_authenticated_just_before_expiry(self):
        db.set_settings({"session_max_days": 1})
        session.refresh_session_settings_cache()

        self.client.cookies.set(session.SESSION_COOKIE, self._cookie_for_age(24 * 60 * 60 - 5))
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)

    def test_json_endpoint_returns_401_once_the_session_expired(self):
        db.set_settings({"session_max_days": 1})
        session.refresh_session_settings_cache()

        self.client.cookies.set(session.SESSION_COOKIE, self._cookie_for_age(24 * 60 * 60))
        response = self.client.get("/api/jobs")

        self.assertEqual(response.status_code, 401)
