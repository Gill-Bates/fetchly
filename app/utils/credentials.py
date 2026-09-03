#!/usr/bin/env python3
#
# app/utils/credentials.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Validation for the single admin account stored in settings.

fetchly ships with authentication switched off and no credentials at all. The
admin username and password are created in Settings → Security and live only in
the ``settings`` table - there is no bootstrap account and no environment
variable that can inject one.

Kept in ``app/utils`` rather than next to the auth routes because ``app/db.py``
validates the stored username on read/write and must not import a route module.
"""

from __future__ import annotations

import re

# Session tokens are colon-delimited (see app/session.py), so a colon in the
# username would corrupt the payload. Whitespace is rejected outright because a
# trailing space in a login name is invisible and unloggable-in. Restricted to
# letters, hyphens and underscores only (case insensitive). Unanchored because
# it is applied with fullmatch(), which - unlike match() with a trailing "$" -
# also rejects a trailing newline.
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
    """Validate a new admin password and return it unchanged.

    Deliberately does not strip: a leading or trailing space is a legitimate
    part of a password, and silently trimming it would lock the user out of the
    credential they think they set.
    """
    password = value or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters")
    return password
