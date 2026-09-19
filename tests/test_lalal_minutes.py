#!/usr/bin/env python3
#
# tests/test_lalal_minutes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Lalal.ai balance: read from the validation call, cached, and reported."""

import unittest
from typing import Any, Self
from unittest.mock import patch

import httpx

from app import db
from app.lalal import LalalError, parse_minutes_left
from tests._support import WebAppTestCase


class FakeLalalClient:
    """Stands in for LalalClient; records how often the API was asked."""

    calls = 0
    quota: Any = {"minutes_left": 261.5}
    error: Exception | None = None

    def __init__(self, api_key: str, *args: object, **kwargs: object) -> None:
        self.api_key = api_key

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def check_quota(self) -> Any:
        type(self).calls += 1
        if type(self).error is not None:
            raise type(self).error
        return type(self).quota


class ParseMinutesLeftTests(unittest.TestCase):
    def test_reads_the_documented_payload(self) -> None:
        self.assertEqual(parse_minutes_left({"minutes_left": 261.5}), 261.5)
        # An exhausted account is a real balance, not an unknown one.
        self.assertEqual(parse_minutes_left({"minutes_left": 0}), 0.0)

    def test_anything_unusable_is_unknown_rather_than_zero(self) -> None:
        for payload in (
            {},
            {"minutes_left": None},
            {"minutes_left": "many"},
            {"minutes_left": True},
            {"minutes_left": -3},
            {"mode": "web_session"},
            None,
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_minutes_left(payload))


class LalalStatusMinutesTests(WebAppTestCase):
    def setUp(self) -> None:
        super().setUp()

        FakeLalalClient.calls = 0
        FakeLalalClient.quota = {"minutes_left": 261.5}
        FakeLalalClient.error = None
        client_patcher = patch("app.lalal.LalalClient", FakeLalalClient)
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

        db.set_settings({"lalalaai_email": "user@example.com", "lalalaai_auth_key": "key-123"})

    def _status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        url = "/api/lalal/status?force_refresh=1" if force_refresh else "/api/lalal/status"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_balance_is_reported_and_persisted(self) -> None:
        self.assertEqual(self._status()["remaining_minutes"], 261.5)
        self.assertEqual(db.get_settings()["lalalaai_minutes_left"], 261.5)

    def test_cached_balance_is_served_without_a_second_api_call(self) -> None:
        self._status()
        self.assertEqual(FakeLalalClient.calls, 1)

        self.assertEqual(self._status()["remaining_minutes"], 261.5)
        self.assertEqual(FakeLalalClient.calls, 1)

    def test_force_refresh_reads_the_balance_again(self) -> None:
        self._status()
        FakeLalalClient.quota = {"minutes_left": 12.25}

        self.assertEqual(self._status(force_refresh=True)["remaining_minutes"], 12.25)
        self.assertEqual(FakeLalalClient.calls, 2)

    def test_failed_validation_drops_the_balance_instead_of_keeping_a_stale_one(self) -> None:
        self._status()
        FakeLalalClient.error = RuntimeError("invalid key")

        payload = self._status(force_refresh=True)
        self.assertFalse(payload["token_valid"])
        self.assertIsNone(payload["remaining_minutes"])
        self.assertEqual(db.get_settings()["lalalaai_minutes_left"], -1)

    def test_a_rejected_credential_is_reported_as_invalid(self) -> None:
        self._status()
        FakeLalalClient.error = LalalError("Invalid API key")

        payload = self._status(force_refresh=True)
        self.assertFalse(payload["token_valid"])
        self.assertIsNone(payload["remaining_minutes"])
        self.assertEqual(db.get_settings()["lalalaai_auth_is_valid"], False)

    def test_an_unreachable_provider_keeps_the_last_known_verdict(self) -> None:
        # A DNS/timeout failure is not a statement about the credential; caching
        # "invalid" for the whole TTL would falsely disconnect a working account.
        self._status()
        FakeLalalClient.error = httpx.ConnectError("name resolution failed")

        payload = self._status(force_refresh=True)
        self.assertTrue(payload["token_valid"])
        self.assertEqual(payload["remaining_minutes"], 261.5)
        self.assertEqual(payload["validation_error"], "Lalal.ai is temporarily unavailable")
        self.assertEqual(db.get_settings()["lalalaai_minutes_left"], 261.5)

    def test_a_timeout_keeps_the_last_known_verdict(self) -> None:
        self._status()
        FakeLalalClient.error = TimeoutError()

        payload = self._status(force_refresh=True)
        self.assertTrue(payload["token_valid"])
        self.assertEqual(payload["remaining_minutes"], 261.5)


    def test_a_source_without_a_balance_reports_unknown(self) -> None:
        FakeLalalClient.quota = {"mode": "web_session"}
        payload = self._status()
        self.assertTrue(payload["token_valid"])
        self.assertIsNone(payload["remaining_minutes"])

    def test_unconfigured_account_reports_no_balance(self) -> None:
        db.set_settings({"lalalaai_email": "", "lalalaai_auth_key": ""})
        payload = self._status()
        self.assertFalse(payload["configured"])
        self.assertIsNone(payload["remaining_minutes"])
        self.assertEqual(FakeLalalClient.calls, 0)

    def test_settings_page_renders_the_cached_balance(self) -> None:
        self._status()
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn('"lalal_minutes_left": 261.5', page.text)

    def test_settings_page_keeps_the_element_the_balance_is_written_into(self) -> None:
        # The tile is rendered by app/static/js/settings.js, which writes the
        # account line and the balance into #lalalStatusLine and silently does
        # nothing when that element is missing - as it was once already.
        self.assertIn('id="lalalStatusLine"', self.client.get("/settings").text)


class LalalActivationKeyMinutesTests(WebAppTestCase):
    """Saving a key stamps checked_at, so it must store the balance it just read."""

    def setUp(self) -> None:
        super().setUp()

        FakeLalalClient.calls = 0
        FakeLalalClient.quota = {"minutes_left": 42.0}
        FakeLalalClient.error = None
        client_patcher = patch("app.lalal.LalalClient", FakeLalalClient)
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def _save_key(self, key: str) -> None:
        response = self.client.post(
            "/api/lalal/auth/activation-key",
            json={"email": "user@example.com", "activation_key": key},
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 200)

    def test_saving_a_key_persists_the_balance_it_validated_with(self) -> None:
        self._save_key("key-123")
        self.assertEqual(db.get_settings()["lalalaai_minutes_left"], 42.0)

        # Within the validation TTL /status must not call out again, so the
        # stored value is what the UI shows.
        calls_after_save = FakeLalalClient.calls
        payload = self.client.get("/api/lalal/status").json()
        self.assertEqual(FakeLalalClient.calls, calls_after_save)
        self.assertEqual(payload["remaining_minutes"], 42.0)

    def test_logging_out_clears_the_balance(self) -> None:
        self._save_key("key-123")
        response = self.client.post(
            "/api/lalal/auth/logout",
            headers={"X-CSRF-Token": self._csrf()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.get_settings()["lalalaai_minutes_left"], -1)

    def test_a_new_key_does_not_inherit_the_previous_balance(self) -> None:
        self._save_key("key-123")
        FakeLalalClient.quota = {"mode": "web_session"}

        self._save_key("key-456")
        self.assertEqual(db.get_settings()["lalalaai_minutes_left"], -1)
        self.assertIsNone(self.client.get("/api/lalal/status").json()["remaining_minutes"])
