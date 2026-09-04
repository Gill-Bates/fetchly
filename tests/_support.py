#!/usr/bin/env python3
#
# tests/_support.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared test setup (see DRY audit finding 10).

Two duplicated setup sequences were previously copy-pasted across ~20 test
files: a ``FETCHLY_SECRET_KEY`` bootstrap that has to run before any
``app.*`` import (session.py/main.py require it at import time), and a
6-step "isolated throwaway SQLite database" dance repeated with a
non-obvious memoization gotcha (``db._database_path_prepared``).

This module must be imported before any ``app.*`` import in a test file that
touches ``app.session``/``app.main`` (directly or transitively): the
``os.environ.setdefault`` call below has to run first. ``tests/conftest.py``
imports this module for every pytest run, so a test file that only needs the
secret key (not the DB isolation classes) no longer needs its own
``os.environ.setdefault`` line at all. The same call is repeated here (not
just in conftest.py) so a file executed outside pytest - e.g.
``python tests/test_foo.py`` - still gets the default before its own
``from app import ...`` line, as long as it imports this module first.
"""

from __future__ import annotations

import os

# Must run before any `from app import ...`: app/session.py and app/main.py
# raise RuntimeError at import time if this is unset. One shared value is
# enough - no test asserts anything about the key's content, only that
# signing/verification is consistent within one test process.
os.environ.setdefault("FETCHLY_SECRET_KEY", "test-shared-secret")

import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from app import db

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


class IsolatedDbTestCase(unittest.TestCase):
    """Point db.DB_PATH at a throwaway, per-test SQLite database.

    Bundles the tempdir + DB_PATH patch + ``_database_path_prepared`` reset +
    close_db/init_db sequence that was previously duplicated across test
    files, including the memoization gotcha: ``_prepare_database_path()``
    caches the parent-directory setup, so a stale cache would skip creating
    the patched path's parent for this test.
    """

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        patcher = patch.object(db, "DB_PATH", Path(self._tmp.name) / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)

        db._database_path_prepared = None
        self.addCleanup(setattr, db, "_database_path_prepared", None)

        self.addCleanup(db.close_db)
        db.init_db()


class WebAppTestCase(IsolatedDbTestCase):
    """An IsolatedDbTestCase with a ready FastAPI TestClient.

    Runs the app-startup steps app/main.py's lifespan handler normally does
    (init_api, refresh_session_settings_cache) and resets the process-wide
    rate limiter, since TestClient does not run the lifespan handler and the
    limiter's state would otherwise leak between test classes.
    """

    client: TestClient

    def setUp(self) -> None:
        super().setUp()

        from fastapi.testclient import TestClient

        from app.common.rate_limit import limiter
        from app.main import app, templates
        from app.routes.api import init_api
        from app.session import refresh_session_settings_cache

        init_api(templates)
        refresh_session_settings_cache()

        limiter.reset()
        self.addCleanup(limiter.reset)

        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def _csrf(self) -> str:
        """Prime the CSRF cookie by loading a page, then return its value."""
        self.client.get("/login")
        token = self.client.cookies.get("fetchly_csrf")
        assert token, "CSRF cookie was not issued"
        return str(token)

    def _post_settings(self, body: dict[str, object]):
        return self.client.post(
            "/api/settings",
            json=body,
            headers={"X-CSRF-Token": self._csrf()},
        )
