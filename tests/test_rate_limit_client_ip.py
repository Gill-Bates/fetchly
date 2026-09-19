#!/usr/bin/env python3
#
# tests/test_rate_limit_client_ip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Client-IP resolution for rate limiting behind a trusted proxy."""

import unittest

from starlette.requests import Request

from app.common.rate_limit import get_rate_limit_ip

# Matches the default FORWARDED_ALLOW_IPS the test process runs with.
_TRUSTED_PEER = "127.0.0.1"
_UNTRUSTED_PEER = "198.51.100.4"


def _request(peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 54321),
    })


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
        for forwarded_for in ("[203.0.113.7]junk", "[2001:db8::1]:not-a-port", "[2001:db8::1"):
            with self.subTest(forwarded_for=forwarded_for):
                self.assertEqual(get_rate_limit_ip(_request(_TRUSTED_PEER, forwarded_for)), _TRUSTED_PEER)

    def test_one_malformed_entry_invalidates_the_whole_chain(self) -> None:
        self.assertEqual(
            get_rate_limit_ip(_request(_TRUSTED_PEER, "not-an-ip, 203.0.113.7")),
            _TRUSTED_PEER,
        )


if __name__ == "__main__":
    unittest.main()
