#!/usr/bin/env python3
#
# tests/test_lalal_minutes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Lalal.ai balance: read from the validation call, cached, and reported."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-lalal-minutes-secret")

from fastapi.testclient import TestClient

from app import db
from app.lalal import parse_minutes_left
from app.main import app, templates
from app.routes.api import init_api
from app.session import refresh_session_settings_cache


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


class LalalStatusMinutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        patcher = patch.object(db, "DB_PATH", Path(self._tmp.name) / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db._database_path_prepared = None
        self.addCleanup(setattr, db, "_database_path_prepared", None)
        self.addCleanup(db.close_db)
        db.init_db()

        init_api(templates)
        refresh_session_settings_cache()

        from app.common.rate_limit import limiter

        limiter.reset()
        self.addCleanup(limiter.reset)

        FakeLalalClient.calls = 0
        FakeLalalClient.quota = {"minutes_left": 261.5}
        FakeLalalClient.error = None
        client_patcher = patch("app.lalal.LalalClient", FakeLalalClient)
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

        db.set_settings({"lalalaai_email": "user@example.com", "lalalaai_auth_key": "key-123"})

        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

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


if __name__ == "__main__":
    unittest.main()
