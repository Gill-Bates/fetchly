#!/usr/bin/env python3
#
# tests/test_rate_limit_client_ip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Client-IP resolution for rate limiting behind a trusted proxy."""

import unittest

from starlette.requests import Request

from app.auth.rate_limit import get_rate_limit_ip, get_rate_limit_key

# Matches the default FORWARDED_ALLOW_IPS the test process runs with.
_TRUSTED_PEER = "127.0.0.1"
_UNTRUSTED_PEER = "198.51.100.4"


def _request(peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 54321),
        }
    )


class RateLimitClientIpTests(unittest.TestCase):
    def test_forwarded_client_is_used_behind_a_trusted_peer(self) -> None:
        self.assertEqual(get_rate_limit_ip(_request(_TRUSTED_PEER, "203.0.113.7")), "203.0.113.7")

    def test_bracketed_ipv6_with_a_port_is_accepted(self) -> None:
        self.assertEqual(
            get_rate_limit_ip(_request(_TRUSTED_PEER, "[2001:db8::1]:41234")),
            "2001:db8::1",
        )

    def test_forwarded_header_is_ignored_for_an_untrusted_peer(self) -> None:
        self.assertEqual(get_rate_limit_ip(_request(_UNTRUSTED_PEER, "203.0.113.7")), _UNTRUSTED_PEER)

    def test_trailing_garbage_after_the_bracket_is_rejected(self) -> None:
        """A malformed entry must fall back to the socket peer rather than be
        trimmed into a valid-looking, attacker-chosen rate-limit key.
        """
        for forwarded_for in (
            "[203.0.113.7]junk",
            "[2001:db8::1]:not-a-port",
            "[2001:db8::1",
        ):
            with self.subTest(forwarded_for=forwarded_for):
                self.assertEqual(
                    get_rate_limit_ip(_request(_TRUSTED_PEER, forwarded_for)),
                    _TRUSTED_PEER,
                )

    def test_one_malformed_entry_invalidates_the_whole_chain(self) -> None:
        self.assertEqual(
            get_rate_limit_ip(_request(_TRUSTED_PEER, "not-an-ip, 203.0.113.7")),
            _TRUSTED_PEER,
        )


class RateLimitBucketKeyTests(unittest.TestCase):
    def _key(self, client: str) -> str:
        return get_rate_limit_key(_request(_TRUSTED_PEER, client))

    def test_addresses_in_one_ipv6_64_share_a_bucket(self) -> None:
        self.assertEqual(self._key("2001:db8:1:2::1"), self._key("2001:db8:1:2:ffff:ffff:ffff:ffff"))
        self.assertEqual(self._key("2001:db8:1:2::1"), "2001:db8:1:2::/64")

    def test_a_different_ipv6_64_gets_its_own_bucket(self) -> None:
        self.assertNotEqual(self._key("2001:db8:1:2::1"), self._key("2001:db8:1:3::1"))

    def test_ipv4_is_unchanged(self) -> None:
        self.assertEqual(self._key("203.0.113.7"), "203.0.113.7")
        self.assertNotEqual(self._key("203.0.113.7"), self._key("203.0.113.8"))

    def test_ipv4_mapped_ipv6_uses_the_ipv4_bucket(self) -> None:
        self.assertEqual(self._key("::ffff:203.0.113.7"), "203.0.113.7")

    def test_stored_client_ip_stays_the_full_address(self) -> None:
        request = _request(_TRUSTED_PEER, "2001:db8:1:2::1")
        self.assertEqual(get_rate_limit_ip(request), "2001:db8:1:2::1")

    def test_unresolvable_client_keeps_the_unknown_key(self) -> None:
        self.assertEqual(get_rate_limit_key(_request("not-an-ip")), "unknown")


if __name__ == "__main__":
    unittest.main()
