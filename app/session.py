#!/usr/bin/env python3
#
# app/session.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Session management with an absolute lifetime counted from login.

import base64
import binascii
import hmac
import logging
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from time import time
from typing import Any, Final

from fastapi import Request, Response

from .db import SESSION_MAX_DAYS_MAX, SESSION_MAX_DAYS_MIN, get_settings

SESSION_COOKIE: Final = "fetchly_session"

# Absolute session lifetime, configurable via the session_max_days setting
# (bounds: SESSION_MAX_DAYS_MIN/MAX, defined in db.py so app/routes/api.py and
# the settings parser share the same range). Counted from login and never
# extended: once it elapses the session is invalid, no matter how active the
# client was (see _is_session_expired).
# Fallback for session_max_days when the setting is unreadable or unset.
_DEFAULT_MAX_DAYS: Final = SESSION_MAX_DAYS_MAX

_SECRET_KEY = os.environ.get("FETCHLY_SECRET_KEY", "")
if not _SECRET_KEY:
    raise RuntimeError("FETCHLY_SECRET_KEY is not set. Cannot start with an empty session signing key.")
_SECRET_KEY_BYTES = _SECRET_KEY.encode("utf-8")
_COOKIE_SECURE_ENV: Final = "FETCHLY_BEHIND_HTTPS"
_SESSION_SETTINGS_DEFAULTS: Final[dict[str, Any]] = {
    "session_version": 0,
    "session_max_days": _DEFAULT_MAX_DAYS,
    # Fail closed until the first cache refresh actually runs: a login gate
    # that briefly reads as "on" is safe, a fresh install briefly reading as
    # "off" is not. app/main.py:init_auth() refreshes this synchronously
    # during the lifespan startup, before any request is served.
    "enable_authentication": True,
}
_SESSION_SETTINGS_CACHE: dict[str, Any] = dict(_SESSION_SETTINGS_DEFAULTS)
_SESSION_SETTINGS_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionData:
    """Parsed session token data."""
    username: str
    issued_at: int  # Unix timestamp of login; the absolute lifetime is measured from here
    nonce: str
    session_version: int


def _encode_token(payload: str, signature: str) -> str:
    """Return an unpadded base64url token encoding ``{payload}:{signature}``.

    The payload itself is colon-delimited and currently stores the username,
    issued-at timestamp, nonce, and session version.
    """
    raw = f"{payload}:{signature}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _is_session_expired(session: SessionData, now: int) -> bool:
    """Return True once the absolute lifetime since login has elapsed.

    Nothing extends it - there is no renewal, so a session dies at
    ``issued_at + session_max_days`` and the user has to sign in again.
    """
    return now >= session.issued_at + _get_max_lifetime_seconds()


def refresh_session_settings_cache() -> None:
    """Refresh the small session-related settings snapshot from the database."""
    try:
        settings = get_settings(include_internal=True)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Failed to refresh session settings cache: %s", exc)
        return

    refreshed = {
        "session_version": settings.get("session_version", 0),
        "session_max_days": settings.get("session_max_days", _DEFAULT_MAX_DAYS),
        "enable_authentication": bool(settings.get("enable_authentication", True)),
    }
    with _SESSION_SETTINGS_LOCK:
        _SESSION_SETTINGS_CACHE.clear()
        _SESSION_SETTINGS_CACHE.update(refreshed)


def _get_cached_session_setting(key: str, default: Any) -> Any:
    with _SESSION_SETTINGS_LOCK:
        return _SESSION_SETTINGS_CACHE.get(key, default)


def _get_max_lifetime_seconds() -> int:
    """Return the validated absolute session lifetime in seconds."""
    try:
        days = int(_get_cached_session_setting("session_max_days", _DEFAULT_MAX_DAYS))
    except (TypeError, ValueError):
        days = _DEFAULT_MAX_DAYS
    return max(SESSION_MAX_DAYS_MIN, min(days, SESSION_MAX_DAYS_MAX)) * 24 * 60 * 60


def _get_session_version() -> int:
    """Return the cached session version used for global session invalidation."""
    try:
        version = int(_get_cached_session_setting("session_version", 0) or 0)
    except (TypeError, ValueError) as exc:
        logger.warning("Failed to parse cached session_version: %s", exc)
        return 0
    return max(0, version)


def get_cached_authentication_enabled() -> bool:
    """Return the cached ``enable_authentication`` flag without touching sqlite.

    Backs ``app.routes.auth.current_user()``, which async route handlers call
    directly (not through ``Depends``) on every HTML/SSE request. Reading
    through the cache here, rather than ``db.get_settings()``, is what keeps
    those call sites off the event loop. Refreshed by
    ``refresh_session_settings_cache()`` at startup, on every settings write,
    and by a periodic background task (see app/main.py).
    """
    return bool(_get_cached_session_setting("enable_authentication", True))


def _sign_payload(payload: str) -> str:
    return hmac.digest(_SECRET_KEY_BYTES, payload.encode("utf-8"), "sha256").hex()


def _validate_live_session(session: SessionData, now: int) -> bool:
    """Return True when a parsed session is still current and accepted."""
    if _is_session_expired(session, now):
        return False
    return session.session_version == _get_session_version()


def create_session(username: str) -> str:
    """Create a new session token for a user.

    Token format: username:issued_at:nonce:session_version:signature
    - issued_at: Login time; the absolute lifetime is measured from here
    """
    if ":" in username:
        raise ValueError("username must not contain ':'")
    now = int(time())
    nonce = secrets.token_urlsafe(12)
    session_version = _get_session_version()
    payload = f"{username}:{now}:{nonce}:{session_version}"
    return _encode_token(payload, _sign_payload(payload))


def parse_session(token: str | None) -> SessionData | None:
    """Parse and validate a session token.

    Returns SessionData when the unpadded base64 token has the expected
    five-part structure and its signature matches. This does not check expiry;
    use validate_session() for full validation.
    """
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = raw.split(":")

        if len(parts) != 5:
            return None

        username, issued_at_str, nonce, session_version_str, sig = parts
        payload = f"{username}:{issued_at_str}:{nonce}:{session_version_str}"

        # Authenticity first: nothing from the token is interpreted before the
        # signature over the whole payload has been verified.
        expected = _sign_payload(payload)
        if not hmac.compare_digest(sig, expected):
            return None

        return SessionData(
            username=username,
            issued_at=int(issued_at_str),
            nonce=nonce,
            session_version=max(0, int(session_version_str)),
        )
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        logger.debug("Session parsing failed", exc_info=True)
        return None


def validate_session(token: str | None) -> str | None:
    """Return the username for a valid session, else None.

    Valid means: signature checks out, issued_at within the configured
    absolute lifetime (session_max_days), and the session version is current.
    """
    session = parse_session(token)
    if not session:
        return None

    now = int(time())
    if not _validate_live_session(session, now):
        return None

    return session.username


def _get_cookie_max_age(token: str) -> int:
    """Return the browser cookie lifetime in seconds for a still-valid token."""
    session = parse_session(token)
    if not session:
        raise ValueError("Cannot set cookie for an invalid session token")

    now = int(time())
    if not _validate_live_session(session, now):
        raise ValueError("Cannot set cookie for an expired session token")

    # Never outlive the server-side check: the cookie expires with the session.
    return session.issued_at + _get_max_lifetime_seconds() - now


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    max_age = _get_cookie_max_age(token)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=_resolve_cookie_secure(request),
        samesite="lax",
        path="/",
    )


def _resolve_cookie_secure(request: Request | None = None, *, secure: bool | None = None) -> bool:
    if secure is not None:
        return secure
    configured_secure = str(os.environ.get(_COOKIE_SECURE_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if request is not None:
        return configured_secure or request.url.scheme == "https"
    return configured_secure


def delete_session_cookie(
    response: Response,
    request: Request | None = None,
    *,
    secure: bool | None = None,
) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_resolve_cookie_secure(request, secure=secure),
        httponly=True,
        samesite="lax",
    )
