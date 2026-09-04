#!/usr/bin/env python3
#
# app/utils/credentials.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Validation for the single admin account stored in settings.

The admin username and password are created in Settings → Security and live
only in the ``settings`` table. Kept in ``app/utils`` so ``app/db.py`` (which
validates the username on read/write) need not import a route module.
"""

from __future__ import annotations

import re

# Letters/hyphens/underscores only: a colon would corrupt the session token
# (colon-delimited), whitespace is invisible in a login name. Unanchored -
# fullmatch() also rejects a trailing newline (unlike match() + "$").
_USERNAME_RE = re.compile(r"[A-Za-z_-]+")

USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 1024


def normalize_admin_username(value: str | None) -> str:
    """Validate and normalise the admin username.

    Returns ``""`` when nothing is configured (the fresh-install state). Raises
    :class:`ValueError` with a user-facing message for anything unusable.
    """
    username = (value or "").strip()
    if not username:
        return ""

    if len(username) > USERNAME_MAX_LENGTH:
        raise ValueError(f"Username must be at most {USERNAME_MAX_LENGTH} characters")
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError(
            "Username may only contain letters, hyphens and underscores"
        )
    return username


def validate_admin_password(value: str | None) -> str:
    """Validate a new admin password and return it unchanged (never stripped -
    leading/trailing spaces are part of the password).
    """
    password = value or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters")
    return password
