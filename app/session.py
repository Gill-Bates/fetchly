#!/usr/bin/env python3
#
# app/session.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Sliding-window session management with idle timeout and hard expiry.
#

from __future__ import annotations

import base64
import hmac
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from time import monotonic, time
from typing import TYPE_CHECKING, Final, TypedDict

if TYPE_CHECKING:
    from fastapi import Request, Response

from .db import get_settings

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SESSION_COOKIE: Final = "tubeyou_session"

# Hard session limit: 24 hours from login (non-configurable)
SESSION_HARD_LIMIT_SECONDS: Final = 24 * 60 * 60

# Default idle timeout (overridden by setting session_idle_minutes)
_DEFAULT_IDLE_MINUTES: Final = 60

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY", "")
if not _SECRET_KEY:
    raise RuntimeError("TUBEYOU_SECRET_KEY is not set. Cannot start with an empty session signing key.")
_SECRET_KEY_BYTES = _SECRET_KEY.encode("utf-8")
_IDLE_TIMEOUT_CACHE_TTL: Final = 60.0
_IDLE_TIMEOUT_CACHE: tuple[float, int] | None = None
_IDLE_TIMEOUT_CACHE_LOCK = threading.Lock()

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


class SessionInfo(TypedDict):
    username: str
    issued_at: int
    last_activity: int
    hard_expires_at: int
    idle_expires_at: int


def _encode_token(payload: str, signature: str) -> str:
    """Return a base64url-encoded payload:signature token without padding."""
    raw = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _is_session_expired(session: SessionData, now: int) -> bool:
    """Return True if hard expiry or sliding idle timeout has been exceeded."""
    if now - session.issued_at > SESSION_HARD_LIMIT_SECONDS:
        return True
    return now - session.last_activity > _get_idle_timeout_seconds()


def _get_idle_timeout_seconds() -> int:
    """Get idle timeout from settings (in seconds)."""
    global _IDLE_TIMEOUT_CACHE

    now = monotonic()
    with _IDLE_TIMEOUT_CACHE_LOCK:
        if _IDLE_TIMEOUT_CACHE is not None:
            cached_at, cached_value = _IDLE_TIMEOUT_CACHE
            if now - cached_at < _IDLE_TIMEOUT_CACHE_TTL:
                return cached_value

    try:
        minutes = int(get_settings().get("session_idle_minutes", _DEFAULT_IDLE_MINUTES))
        result = max(5, min(minutes, 1440)) * 60
    except Exception as exc:
        logger.warning("Failed to read session_idle_minutes from settings: %s", exc)
        result = _DEFAULT_IDLE_MINUTES * 60

    with _IDLE_TIMEOUT_CACHE_LOCK:
        _IDLE_TIMEOUT_CACHE = (now, result)
    return result


def _sign_payload(payload: str) -> str:
    """Create HMAC signature for payload."""
    return hmac.digest(_SECRET_KEY_BYTES, payload.encode("utf-8"), "sha256").hex()


def create_session(username: str) -> str:
    """Create a new session token for a user.
    
    Token format: username:issued_at:last_activity:nonce:signature
    - issued_at: Login time (for 24h hard limit)
    - last_activity: Last request time (for sliding idle timeout)
    """
    now = int(time())
    nonce = secrets.token_urlsafe(12)
    payload = f"{username}:{now}:{now}:{nonce}"
    return _encode_token(payload, _sign_payload(payload))


def parse_session(token: str | None) -> SessionData | None:
    """Parse and validate a session token.
    
    Returns SessionData if token is structurally valid and signature matches.
    Does NOT check expiry - use validate_session() for full validation.
    """
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = raw.split(":")

        if len(parts) != 5:
            return None

        username, issued_at_str, last_activity_str, nonce, sig = parts
        payload = f"{username}:{issued_at_str}:{last_activity_str}:{nonce}"
        last_activity = int(last_activity_str)
        
        expected = _sign_payload(payload)
        if not hmac.compare_digest(sig, expected):
            return None
        
        return SessionData(
            username=username,
            issued_at=int(issued_at_str),
            last_activity=last_activity,
            nonce=nonce,
        )
    except Exception:
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
    if _is_session_expired(session, now):
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
    if _is_session_expired(session, now):
        return None

    nonce = secrets.token_urlsafe(12)
    payload = f"{session.username}:{session.issued_at}:{now}:{nonce}"
    return _encode_token(payload, _sign_payload(payload))


def get_session_info(token: str | None) -> SessionInfo | None:
    """Return detailed session metadata for display or debugging.

    Returns None only if the token cannot be parsed or the signature is invalid.
    This does not check expiry; callers must compare the returned timestamps
    against the current time themselves.
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
def authenticated_user(token: str | None) -> str | None:
    """Get authenticated username from token."""
    return validate_session(token)


def set_session_cookie(response: Response, token: str, request: "Request") -> None:
    """Set session cookie on response with appropriate security flags."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def delete_session_cookie(response: Response, request: "Request") -> None:
    """Delete session cookie from response."""
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
