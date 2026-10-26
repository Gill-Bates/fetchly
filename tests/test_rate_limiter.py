#!/usr/bin/env python3
#
# tests/test_rate_limiter.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The in-house sliding-window rate limiter and its wiring into the routes."""

import contextlib
import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import rate_limit as rl
from app.auth.rate_limit import SlidingWindow, parse_rate_spec, rate_limit, reset_rate_limits
from tests._support import WebAppTestCase

_TRUSTED_PEER = "127.0.0.1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _request(peer: str, forwarded_for: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded_for.encode())] if forwarded_for else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1)})


class SlidingWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()

    def test_window_slides_without_a_double_burst_at_the_boundary(self) -> None:
        window = SlidingWindow(2, 60.0, self.clock)
        self.assertIsNone(window.hit("a"))
        self.clock.now += 50
        self.assertIsNone(window.hit("a"))
        self.clock.now += 20  # first hit left the window, second did not
        self.assertIsNone(window.hit("a"))
        self.assertIsNotNone(window.hit("a"))

    def test_rejections_are_not_recorded_and_report_the_wait(self) -> None:
        window = SlidingWindow(1, 60.0, self.clock)
        self.assertIsNone(window.hit("a"))
        for _ in range(5):
            self.clock.now += 5
            self.assertIsNotNone(window.hit("a"))
        self.clock.now = 1000.0 + 60.0
        self.assertIsNone(window.hit("a"))

    def test_flooding_one_key_never_rejects_another(self) -> None:
        window = SlidingWindow(1, 60.0, self.clock)
        for _ in range(100):
            window.hit("attacker")
        self.assertIsNone(window.hit("victim"))

    def test_key_flood_evicts_least_recently_used_and_stays_bounded(self) -> None:
        window = SlidingWindow(1, 60.0, self.clock, max_keys=5)
        for i in range(500):
            self.assertIsNone(window.hit(f"k{i}"))
            self.assertLessEqual(len(window._hits), 5)
        self.assertIsNone(window.hit("brand-new-legit-client"))

    def test_reset_forgets_counters(self) -> None:
        window = SlidingWindow(1, 60.0, self.clock)
        window.hit("a")
        window.reset()
        self.assertIsNone(window.hit("a"))


class RateLimitDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limits()
        self.addCleanup(reset_rate_limits)

    def test_specs_are_parsed_and_bad_ones_rejected(self) -> None:
        self.assertEqual(parse_rate_spec("5/minute"), (5, 60.0))
        self.assertEqual(parse_rate_spec("2/hour"), (2, 3600.0))
        for bad in ("", "5", "0/minute", "5/day", "x/minute", "5/minutes"):
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                parse_rate_spec(bad)

    def test_exceeding_raises_429_with_retry_after(self) -> None:
        dep = rate_limit("1/minute")
        dep(_request(_TRUSTED_PEER))
        with self.assertRaises(HTTPException) as ctx:
            dep(_request(_TRUSTED_PEER))
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail, "Rate limit exceeded")
        self.assertGreaterEqual(int(ctx.exception.headers["Retry-After"]), 1)

    def test_routes_have_separate_budgets(self) -> None:
        first, second = rate_limit("1/minute"), rate_limit("1/minute")
        first(_request(_TRUSTED_PEER))
        second(_request(_TRUSTED_PEER))

    def test_ipv6_rotation_inside_a_64_shares_one_bucket(self) -> None:
        dep = rate_limit("1/minute")
        dep(_request("2001:db8:1:2::1"))
        with self.assertRaises(HTTPException):
            dep(_request("2001:db8:1:2:ffff::9"))
        dep(_request("2001:db8:1:3::1"))  # another /64 is unaffected

    def test_flooding_ip_a_does_not_throttle_ip_b(self) -> None:
        dep = rate_limit("2/minute")
        for _ in range(50):
            with contextlib.suppress(HTTPException):
                dep(_request("203.0.113.1"))
        dep(_request("203.0.113.2"))

    def test_spoofed_forwarded_for_from_untrusted_peer_cannot_pick_a_victim_bucket(self) -> None:
        dep = rate_limit("1/minute")
        for _ in range(3):
            with contextlib.suppress(HTTPException):
                dep(_request("198.51.100.4", forwarded_for="203.0.113.7"))
        dep(_request(_TRUSTED_PEER, forwarded_for="203.0.113.7"))

    def test_unexpected_limiter_failure_fails_closed(self) -> None:
        dep = rate_limit("5/minute")
        original = rl.get_rate_limit_key
        rl.get_rate_limit_key = lambda _request: 1 / 0  # type: ignore[assignment]
        self.addCleanup(setattr, rl, "get_rate_limit_key", original)
        with self.assertLogs(rl.logger, "ERROR"), self.assertRaises(HTTPException) as ctx:
            dep(_request(_TRUSTED_PEER))
        self.assertEqual(ctx.exception.status_code, 429)


class RateLimitedRoutesTests(WebAppTestCase):
    def test_spoofed_header_attacker_cannot_lock_out_the_victim_at_login(self) -> None:
        attacker = TestClient(self.client.app, follow_redirects=False, client=("198.51.100.4", 5000))
        self.addCleanup(attacker.close)
        statuses = []
        attacker.get("/login")
        token = attacker.cookies.get("fetchly_csrf") or ""
        for _ in range(8):
            r = attacker.post(
                "/login",
                json={"username": "a", "password": "b"},
                headers={"X-CSRF-Token": token, "X-Forwarded-For": "203.0.113.7"},
            )
            statuses.append(r.status_code)
        self.assertIn(429, statuses)
        self.assertEqual(statuses[0] != 429, True)

        victim = TestClient(self.client.app, follow_redirects=False, client=("203.0.113.7", 5000))
        self.addCleanup(victim.close)
        victim.get("/login")
        r = victim.post(
            "/login",
            json={"username": "a", "password": "b"},
            headers={"X-CSRF-Token": victim.cookies.get("fetchly_csrf") or ""},
        )
        self.assertNotEqual(r.status_code, 429)

    def test_every_expected_route_is_rate_limited(self) -> None:
        from app import routes as route_pkg

        found: set[tuple[str, str, str]] = set()
        for name in dir(route_pkg):
            if not name.endswith("_router"):
                continue
            for route in getattr(route_pkg, name).routes:
                for dep in route.dependant.dependencies:
                    spec = getattr(dep.call, "rate_limit_spec", None)
                    if spec:
                        found.update((m, route.path, spec) for m in route.methods - {"HEAD"})
        self.assertEqual(found, _EXPECTED_LIMITED_ROUTES)


_EXPECTED_LIMITED_ROUTES = {
    ("GET", "/api/cookies", "30/minute"),
    ("DELETE", "/api/cookies/{platform}", "10/minute"),
    ("POST", "/api/cookies/{platform}", "10/minute"),
    ("POST", "/api/cookies/{platform}/paste", "10/minute"),
    ("GET", "/api/info", "20/minute"),
    ("GET", "/api/jobs", "60/minute"),
    ("POST", "/api/jobs/remove-all", "2/minute"),
    ("GET", "/api/jobs/{job_id}", "60/minute"),
    ("POST", "/api/jobs/{job_id}/cancel", "30/minute"),
    ("GET", "/api/jobs/{job_id}/events", "30/minute"),
    ("POST", "/api/jobs/{job_id}/retry", "10/minute"),
    ("POST", "/api/lalal/auth/activation-key", "5/minute"),
    ("POST", "/api/lalal/auth/logout", "10/minute"),
    ("GET", "/api/lalal/download/{job_id}", "10/minute"),
    ("GET", "/api/lalal/status", "30/minute"),
    ("POST", "/api/lalal/{job_id}", "5/minute"),
    ("GET", "/api/settings", "60/minute"),
    ("POST", "/api/settings", "5/minute"),
    ("DELETE", "/api/settings/watermark-logo", "10/minute"),
    ("GET", "/api/settings/watermark-logo", "60/minute"),
    ("POST", "/api/settings/watermark-logo", "10/minute"),
    ("GET", "/api/settings/watermark-logo/image", "60/minute"),
    ("POST", "/api/share/{job_id}", "20/minute"),
    ("GET", "/api/stats", "30/minute"),
    ("GET", "/api/stats/bpm-clusters", "30/minute"),
    ("POST", "/api/stats/reset", "5/minute"),
    ("POST", "/api/submit", "10/minute"),
    ("GET", "/api/system/host", "60/minute"),
    ("GET", "/api/thumbnail-cache/{cache_key}", "120/minute"),
    ("GET", "/api/thumbnail-proxy", "30/minute"),
    ("GET", "/api/thumbnail/resolve", "30/minute"),
    ("DELETE", "/api/trim/{job_id}", "30/minute"),
    ("POST", "/api/trim/{job_id}", "10/minute"),
    ("GET", "/api/trim/{job_id}/{trim_id}/download", "30/minute"),
    ("GET", "/api/updates", "30/minute"),
    ("GET", "/audio-source/{job_id}", "60/minute"),
    ("GET", "/download/{job_id}", "30/minute"),
    ("GET", "/events", "30/minute"),
    ("GET", "/job/{job_id}", "120/minute"),
    ("GET", "/login", "20/minute"),
    ("POST", "/login", "5/minute"),
    ("POST", "/logout", "20/minute"),
    ("GET", "/share/{token}", "20/minute"),
    ("GET", "/thumbnail/{job_id}", "120/minute"),
}
