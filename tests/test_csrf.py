#!/usr/bin/env python3
#
# tests/test_csrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CSRF protection: the double-submit token check, and the additive
Sec-Fetch-Site/Origin/Referer cross-site rejection (S8).

The cross-site check is additive, not a replacement: a request still has to
carry a matching token even when it passes as same-origin.
"""

from __future__ import annotations

from unittest.mock import patch

from middleware.csrf import CSRFMiddleware, _effective_port
from tests._support import WebAppTestCase


class DoubleSubmitTokenTests(WebAppTestCase):
    def test_missing_token_is_rejected(self):
        response = self.client.post("/api/jobs/remove-all", json={})
        self.assertEqual(response.status_code, 403)

    def test_mismatched_token_is_rejected(self):
        self._csrf()  # primes the cookie
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": "not-the-real-token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_matching_token_passes_the_csrf_layer(self):
        # 400/422/whatever from the handler itself is fine - what matters is
        # that it is not the CSRF middleware's own 403.
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertNotEqual(response.status_code, 403)

    def test_safe_methods_are_never_checked(self):
        # No cookie primed at all - a GET must not be rejected regardless.
        response = self.client.get("/api/jobs")
        self.assertNotEqual(response.status_code, 403)

    def test_a_path_outside_the_protected_prefixes_is_not_checked(self):
        # /health is outside ("/login", "/logout", "/api"): no token, no 403.
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)


class CrossSiteRejectionTests(WebAppTestCase):
    """The Sec-Fetch-Site/Origin/Referer check added on top of the token."""

    def test_sec_fetch_site_cross_site_is_rejected_even_with_a_valid_token(self):
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403)

    def test_sec_fetch_site_same_site_is_rejected_too(self):
        # "same-site" (a sibling subdomain) is deliberately not trusted: that
        # is exactly the cookie-tossing gap the token check alone leaves open.
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "same-site"},
        )
        self.assertEqual(response.status_code, 403)

    def test_sec_fetch_site_same_origin_passes(self):
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertNotEqual(response.status_code, 403)

    def test_sec_fetch_site_none_passes(self):
        # A typed URL/bookmark has no meaningful site; not attacker-controlled.
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "none"},
        )
        self.assertNotEqual(response.status_code, 403)

    def test_a_foreign_origin_is_rejected_when_sec_fetch_site_is_absent(self):
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_a_matching_origin_passes_when_sec_fetch_site_is_absent(self):
        token = self._csrf()
        # TestClient's default base_url is http://testserver.
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "http://testserver"},
        )
        self.assertNotEqual(response.status_code, 403)

    def test_a_null_origin_is_rejected(self):
        # Sandboxed/redirected contexts send "Origin: null" - unverifiable,
        # so it must not be treated as a same-origin pass.
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "null"},
        )
        self.assertEqual(response.status_code, 403)

    def test_no_signal_at_all_is_treated_as_same_origin(self):
        # Absence of Sec-Fetch-Site/Origin/Referer (e.g. an older client) is
        # unknown, not proof of cross-site - the token check still gates it.
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={"X-CSRF-Token": token},
        )
        self.assertNotEqual(response.status_code, 403)


class EffectivePortNormalizationTests(WebAppTestCase):
    """Regression: comparing raw ports false-flagged same-origin requests.

    A proxy that forwards "Host: example.com:443" makes request.url.port
    read 443, while a browser's own Origin header omits the default port
    per RFC 6454. Without normalizing both sides, every such same-origin
    request behind that kind of proxy would have been rejected as
    cross-site - a false-positive lockout, not a security gap.
    """

    def test_explicit_default_port_matches_implicit_default_port(self):
        self.assertEqual(_effective_port("https", 443), _effective_port("https", None))
        self.assertEqual(_effective_port("http", 80), _effective_port("http", None))

    def test_a_genuinely_different_port_still_differs(self):
        self.assertNotEqual(_effective_port("https", 8443), _effective_port("https", None))

    def test_origin_with_explicit_default_port_passes_against_a_proxy_supplied_host(self):
        token = self._csrf()
        response = self.client.post(
            "/api/jobs/remove-all",
            json={},
            headers={
                "X-CSRF-Token": token,
                "Origin": "http://testserver:80",
                "Host": "testserver:80",
            },
        )
        self.assertNotEqual(response.status_code, 403)


class HostPrefixCookieNameTests(WebAppTestCase):
    """__Host-fetchly_csrf requires Secure and no Domain attribute - both must

    hold whenever main.py picks that name, not just when FETCHLY_BEHIND_HTTPS
    happens to be set today.
    """

    def test_the_resolver_names_the_cookie_by_the_secure_flag(self):
        from app.main import _resolve_csrf_cookie_name

        with patch("app.main.resolve_cookie_secure", return_value=True):
            self.assertEqual(_resolve_csrf_cookie_name(), "__Host-fetchly_csrf")
        with patch("app.main.resolve_cookie_secure", return_value=False):
            self.assertEqual(_resolve_csrf_cookie_name(), "fetchly_csrf")

    def test_the_cookie_header_never_carries_a_domain_attribute(self):
        # __Host- is only valid without Domain; _cookie_header() must never
        # add one regardless of the chosen name.
        middleware = CSRFMiddleware(app=None, csrf_cookie_name="__Host-fetchly_csrf", protected_paths=("/api",))
        header = middleware._cookie_header("token-value", secure=True)
        self.assertNotIn("Domain=", header)
        self.assertIn("Secure", header)


class CsrfCookieNameTests(WebAppTestCase):
    def test_cookie_name_construction_rejects_illegal_characters(self):
        with self.assertRaises(ValueError):
            CSRFMiddleware(app=None, csrf_cookie_name="bad;name", protected_paths=("/api",))

    def test_empty_protected_paths_is_rejected(self):
        with self.assertRaises(ValueError):
            CSRFMiddleware(app=None, csrf_cookie_name="csrf", protected_paths=())

    def test_a_relative_protected_path_is_rejected(self):
        with self.assertRaises(ValueError):
            CSRFMiddleware(app=None, csrf_cookie_name="csrf", protected_paths=("api",))
