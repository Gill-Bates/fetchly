#!/usr/bin/env python3
#
# tests/test_public_url.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest
from types import SimpleNamespace

from app import db
from app.utils.public_url import build_public_base_url, normalize_public_hostname
from tests._support import IsolatedDbTestCase


class NormalizePublicHostnameTests(unittest.TestCase):
    def test_blank_returns_empty(self):
        for value in ("", "   ", None):
            self.assertEqual(normalize_public_hostname(value), "")

    def test_hostname_passthrough(self):
        self.assertEqual(
            normalize_public_hostname("  fetchly.example.com "),
            "fetchly.example.com",
        )
        self.assertEqual(normalize_public_hostname("localhost"), "localhost")

    def test_ip_addresses_are_canonicalised(self):
        self.assertEqual(normalize_public_hostname("10.0.0.5"), "10.0.0.5")
        self.assertEqual(normalize_public_hostname("[2001:db8::1]"), "2001:db8::1")

    def test_rejects_scheme_port_and_path(self):
        for value in ("https://x.example", "x.example:8443", "x.example/share"):
            with self.assertRaises(ValueError):
                normalize_public_hostname(value)

    def test_rejects_malformed_labels(self):
        for value in ("-x.example", "x-.example", "x..example", ".x.example", "a b"):
            with self.assertRaises(ValueError):
                normalize_public_hostname(value)

    def test_rejects_overlong_hostname(self):
        with self.assertRaises(ValueError):
            normalize_public_hostname("a" * 254)


class BuildPublicBaseUrlTests(unittest.TestCase):
    def _request(self, base_url: str):
        return SimpleNamespace(base_url=base_url)

    def test_falls_back_to_request_base_url(self):
        req = self._request("http://fetchly:8000/")
        self.assertEqual(build_public_base_url(req, ""), "http://fetchly:8000")

    def test_configured_hostname_forces_https(self):
        req = self._request("http://fetchly:8000/")
        self.assertEqual(
            build_public_base_url(req, "vids.example.com"),
            "https://vids.example.com",
        )

    def test_configured_ipv6_is_bracketed(self):
        req = self._request("http://fetchly:8000/")
        self.assertEqual(
            build_public_base_url(req, "2001:db8::1"),
            "https://[2001:db8::1]",
        )


class SettingsRoundTripTests(IsolatedDbTestCase):
    def test_public_hostname_persists_and_clears(self):
        self.assertEqual(db.get_settings().get("public_hostname"), "")

        db.set_settings({"public_hostname": "share.example.com"})
        self.assertEqual(db.get_settings().get("public_hostname"), "share.example.com")

        db.set_settings({"public_hostname": ""})
        self.assertEqual(db.get_settings().get("public_hostname"), "")

        with self.assertRaises(ValueError):
            db.set_settings({"public_hostname": "bad host"})


if __name__ == "__main__":
    unittest.main()
