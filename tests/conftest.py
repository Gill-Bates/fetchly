#!/usr/bin/env python3
#
# tests/conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pytest-wide setup (see DRY audit finding 10).

Importing tests._support here guarantees FETCHLY_SECRET_KEY is set before
pytest imports any test module - and therefore before any test module's own
`from app import ...` line - regardless of collection order. Test files that
only need the secret key no longer need their own
`os.environ.setdefault("FETCHLY_SECRET_KEY", ...)` line; files that need an
isolated database or a FastAPI TestClient should subclass
tests._support.IsolatedDbTestCase / WebAppTestCase instead of hand-rolling
the same setUp() sequence.
"""

from tests import _support  # noqa: F401  # side effect: sets FETCHLY_SECRET_KEY
