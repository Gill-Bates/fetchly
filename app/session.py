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
import os
import secrets
from dataclasses import dataclass
from hashlib import sha256
from time import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request, Response

from .db import get_settings

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SESSION_COOKIE = "tubeyou_session"

# Hard session limit: 24 hours from login (non-configurable)
SESSION_HARD_LIMIT_SECONDS = 24 * 60 * 60

# Default idle timeout (overridden by setting session_idle_minutes)
_DEFAULT_IDLE_MINUTES = 60

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY", "")


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


def _get_idle_timeout_seconds() -> int:
    """Get idle timeout from settings (in seconds)."""
    try:
        minutes = int(get_settings().get("session_idle_minutes", _DEFAULT_IDLE_MINUTES))
        return max(5, min(minutes, 1440)) * 60  # Clamp between 5 min and 24h
    except Exception:
        return _DEFAULT_IDLE_MINUTES * 60


def _sign_payload(payload: str) -> str:
    """Create HMAC signature for payload."""
    return hmac.new(_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()


def create_session(username: str) -> str:
    """Create a new session token for a user.
    
    Token format: username:issued_at:last_activity:nonce:signature
    - issued_at: Login time (for 24h hard limit)
    - last_activity: Last request time (for sliding idle timeout)
    """
    now = int(time())
    nonce = secrets.token_urlsafe(12)
    payload = f"{username}:{now}:{now}:{nonce}"
    sig = _sign_payload(payload)
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


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
        
        # Support both old (4-part) and new (5-part) token formats
        if len(parts) == 4:
            # Old format: username:issued_at:nonce:sig
            username, issued_at_str, nonce, sig = parts
            payload = f"{username}:{issued_at_str}:{nonce}"
            last_activity = int(issued_at_str)
        elif len(parts) == 5:
            # New format: username:issued_at:last_activity:nonce:sig
            username, issued_at_str, last_activity_str, nonce, sig = parts
            payload = f"{username}:{issued_at_str}:{last_activity_str}:{nonce}"
            last_activity = int(last_activity_str)
        else:
            return None
        
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
    
    # Hard limit: 24 hours from login
    if now - session.issued_at > SESSION_HARD_LIMIT_SECONDS:
        return None
    
    # Sliding idle timeout
    idle_timeout = _get_idle_timeout_seconds()
    if now - session.last_activity > idle_timeout:
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
    
    # Don't renew if hard limit exceeded
    if now - session.issued_at > SESSION_HARD_LIMIT_SECONDS:
        return None
    
    # Don't renew if idle timeout exceeded
    idle_timeout = _get_idle_timeout_seconds()
    if now - session.last_activity > idle_timeout:
        return None
    
    # Create renewed token with same issued_at but updated last_activity
    nonce = secrets.token_urlsafe(12)
    payload = f"{session.username}:{session.issued_at}:{now}:{nonce}"
    sig = _sign_payload(payload)
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def get_session_info(token: str | None) -> dict[str, object] | None:
    """Get detailed session information for debugging/display.
    
    Returns dict with username, issued_at, last_activity, expires_at, idle_expires_at
    or None if token is invalid.
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


def login_required_enabled() -> bool:
    """Check if login is required based on settings."""
    try:
        return bool(get_settings().get("login_required", True))
    except Exception:
        return True


def authenticated_user(token: str | None) -> str | None:
    """Get authenticated username from token, considering login_required setting."""
    user = validate_session(token)
    if user:
        return user
    if not login_required_enabled():
        return "anonymous"
    return None


def set_session_cookie(response: Response, token: str, request: "Request") -> None:
    """Set session cookie on response with appropriate security flags."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=SESSION_HARD_LIMIT_SECONDS,
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
