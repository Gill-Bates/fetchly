#!/usr/bin/env python3
#
# app/session.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Sliding-window session management with idle timeout and hard expiry.
#

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
from typing import Any, Final, TypedDict

from fastapi import Request, Response

from .db import get_settings

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SESSION_COOKIE: Final = "tubeyou_session"

# Hard session limit: 24 hours from login (non-configurable)
SESSION_HARD_LIMIT_SECONDS: Final = 24 * 60 * 60

# Fixed sliding idle timeout (non-configurable)
_DEFAULT_IDLE_MINUTES: Final = 60

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY", "")
if not _SECRET_KEY:
    raise RuntimeError("TUBEYOU_SECRET_KEY is not set. Cannot start with an empty session signing key.")
_SECRET_KEY_BYTES = _SECRET_KEY.encode("utf-8")
_COOKIE_SECURE_ENV: Final = "TUBEYOU_BEHIND_HTTPS"
_SESSION_SETTINGS_DEFAULTS: Final[dict[str, Any]] = {
    "session_version": 0,
    "session_idle_minutes": _DEFAULT_IDLE_MINUTES,
}
_SESSION_SETTINGS_CACHE: dict[str, Any] = dict(_SESSION_SETTINGS_DEFAULTS)
_SESSION_SETTINGS_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Session Token Structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SessionData:
    """Parsed session token data."""
    username: str
    issued_at: int      # Unix timestamp of original login
    last_activity: int  # Unix timestamp of last activity (for sliding window)
    nonce: str
    session_version: int


class SessionInfo(TypedDict):
    username: str
    issued_at: int
    last_activity: int
    hard_expires_at: int
    idle_expires_at: int


def _encode_token(payload: str, signature: str) -> str:
    """Return an unpadded base64url token encoding ``{payload}:{signature}``.

    The payload itself is colon-delimited and currently stores the username,
    issued-at timestamp, last-activity timestamp, nonce, and session version.
    """
    raw = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _is_session_expired(session: SessionData, now: int) -> bool:
    """Return True if hard expiry or sliding idle timeout has been exceeded."""
    if now >= session.issued_at + SESSION_HARD_LIMIT_SECONDS:
        return True
    return now >= session.last_activity + _get_idle_timeout_seconds()


def refresh_session_settings_cache() -> None:
    """Refresh the small session-related settings snapshot from the database."""
    try:
        settings = get_settings(include_internal=True)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Failed to refresh session settings cache: %s", exc)
        return

    refreshed = {
        "session_version": settings.get("session_version", 0),
        "session_idle_minutes": settings.get("session_idle_minutes", _DEFAULT_IDLE_MINUTES),
    }
    with _SESSION_SETTINGS_LOCK:
        _SESSION_SETTINGS_CACHE.clear()
        _SESSION_SETTINGS_CACHE.update(refreshed)


def _get_cached_session_setting(key: str, default: Any) -> Any:
    with _SESSION_SETTINGS_LOCK:
        return _SESSION_SETTINGS_CACHE.get(key, default)


def _get_idle_timeout_seconds() -> int:
    """Return the validated sliding idle timeout in seconds."""
    try:
        minutes = int(_get_cached_session_setting("session_idle_minutes", _DEFAULT_IDLE_MINUTES))
    except (TypeError, ValueError):
        minutes = _DEFAULT_IDLE_MINUTES
    return max(1, min(minutes, 24 * 60)) * 60


def _get_session_version() -> int:
    """Return the cached session version used for global session invalidation."""
    try:
        version = int(_get_cached_session_setting("session_version", 0) or 0)
    except Exception as exc:
        logger.warning("Failed to parse cached session_version: %s", exc)
        return 0
    return max(0, version)


def _sign_payload(payload: str) -> str:
    """Create HMAC signature for payload."""
    return hmac.digest(_SECRET_KEY_BYTES, payload.encode("utf-8"), "sha256").hex()


def _validate_live_session(session: SessionData, now: int) -> bool:
    """Return True when a parsed session is still current and accepted."""
    if _is_session_expired(session, now):
        return False
    return session.session_version == _get_session_version()


def create_session(username: str) -> str:
    """Create a new session token for a user.
    
    Token format: username:issued_at:last_activity:nonce:session_version:signature
    - issued_at: Login time (for 24h hard limit)
    - last_activity: Last request time (for sliding idle timeout)
    """
    if ":" in username:
        raise ValueError("username must not contain ':'")
    now = int(time())
    nonce = secrets.token_urlsafe(12)
    session_version = _get_session_version()
    payload = f"{username}:{now}:{now}:{nonce}:{session_version}"
    return _encode_token(payload, _sign_payload(payload))


def parse_session(token: str | None) -> SessionData | None:
    """Parse and validate a session token.
    
    Returns SessionData when the unpadded base64 token has the expected
    six-part structure and its signature matches. This does not check expiry;
    use validate_session() for full validation.
    """
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = raw.split(":")

        if len(parts) != 6:
            return None

        username, issued_at_str, last_activity_str, nonce, session_version_str, sig = parts
        payload = f"{username}:{issued_at_str}:{last_activity_str}:{nonce}:{session_version_str}"

        last_activity = int(last_activity_str)
        session_version = max(0, int(session_version_str))
        
        expected = _sign_payload(payload)
        if not hmac.compare_digest(sig, expected):
            return None
        
        return SessionData(
            username=username,
            issued_at=int(issued_at_str),
            last_activity=last_activity,
            nonce=nonce,
            session_version=session_version,
        )
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        logger.debug("Session parsing failed", exc_info=True)
        return None


def validate_session(token: str | None) -> str | None:
    """Validate session and return username if valid.
    
    Session is valid if:
    1. Token signature is valid
    2. issued_at is within 24 hours (hard limit)
    3. last_activity is within idle timeout (sliding window)
    
    Returns username if valid, None otherwise.
    """
    session = parse_session(token)
    if not session:
        return None

    now = int(time())
    if not _validate_live_session(session, now):
        return None

    return session.username


def renew_session(token: str | None) -> str | None:
    """Renew session by updating last_activity timestamp.
    
    Returns new token if session is still valid, None if expired.
    This extends the sliding window while preserving the original issued_at.
    """
    session = parse_session(token)
    if not session:
        return None

    now = int(time())
    if not _validate_live_session(session, now):
        return None

    nonce = secrets.token_urlsafe(12)
    payload = f"{session.username}:{session.issued_at}:{now}:{nonce}:{session.session_version}"
    return _encode_token(payload, _sign_payload(payload))


def get_session_info(token: str | None) -> SessionInfo | None:
    """Return session metadata including computed expiry timestamps.

    The token is parsed and signature-validated, but expiry is not evaluated.
    Returns None only when parsing or signature validation fails. Callers must
    compare the returned timestamps against the current time themselves.
    """
    session = parse_session(token)
    if not session:
        return None

    idle_timeout = _get_idle_timeout_seconds()

    return {
        "username": session.username,
        "issued_at": session.issued_at,
        "last_activity": session.last_activity,
        "hard_expires_at": session.issued_at + SESSION_HARD_LIMIT_SECONDS,
        "idle_expires_at": session.last_activity + idle_timeout,
    }


def _get_cookie_max_age(token: str) -> int:
    """Return the browser cookie lifetime in seconds for a still-valid token."""
    session = parse_session(token)
    if not session:
        raise ValueError("Cannot set cookie for an invalid session token")

    now = int(time())
    if not _validate_live_session(session, now):
        raise ValueError("Cannot set cookie for an expired session token")

    hard_remaining = session.issued_at + SESSION_HARD_LIMIT_SECONDS - now
    idle_remaining = session.last_activity + _get_idle_timeout_seconds() - now
    return min(hard_remaining, idle_remaining)


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    """Set session cookie on response with appropriate security flags."""
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
    if request is not None:
        return request.url.scheme == "https"
    return str(os.environ.get(_COOKIE_SECURE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def delete_session_cookie(
    response: Response,
    request: Request | None = None,
    *,
    secure: bool | None = None,
) -> None:
    """Delete session cookie from response."""
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_resolve_cookie_secure(request, secure=secure),
        httponly=True,
        samesite="lax",
    )
