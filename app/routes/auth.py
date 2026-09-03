#!/usr/bin/env python3
#
# app/routes/auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authentication routes and helpers."""

from __future__ import annotations

import asyncio
import hmac
import logging
from hashlib import pbkdf2_hmac, sha256
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..common.rate_limit import limiter
from ..db import get_settings
from ..session import (
    SESSION_COOKIE,
    create_session,
    delete_session_cookie,
    get_cached_authentication_enabled,
    set_session_cookie,
    validate_session,
)
from ..utils.hidden_captcha import (
    HONEYPOT_FIELD_NAME,
    CaptchaOutcome,
    issue_captcha_token,
    verify_captcha_token,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Client-facing message for a failed Hidden_Captcha check. Deliberately
# generic and identical for every failing signal (honeypot filled, missing/
# invalid/expired token, too-fast submit) so a caller can never learn which
# invisible check tripped.
_CAPTCHA_REJECTED_MESSAGE = (
    "We couldn't verify your submission. Please reload the page and try again."
)

# Effective identity while authentication is switched off. fetchly has a single
# admin account and no per-user data ownership, so the authorization
# dependencies just need *some* stable truthy principal to hand back. This is
# never a login name: with authentication off there is no login at all, and
# with it on the real name comes from the admin_username setting.
LOCAL_USER: Final = "local"

# Module-level state (set during init)
_templates: Jinja2Templates | None = None
_SECRET_KEY: str = ""


def _login_template_context(request: Request, **extra: object) -> dict[str, object]:
    """Build template context with CSRF token for login page."""
    context: dict[str, object] = {"csrf_token": getattr(request.state, "csrf_token", "")}
    context.update(extra)
    return context


def init_auth(templates: Jinja2Templates, secret_key: str) -> None:
    """Initialize the auth module with required dependencies.

    Must be called before routes are used. There is deliberately no bootstrap
    credential to pass in: the admin account is created in Settings → Security
    and lives only in the database.
    """
    global _templates, _SECRET_KEY
    _templates = templates
    _SECRET_KEY = secret_key


def _derive_salt(username: str) -> bytes:
    """Derive a salt from the secret key and username."""
    return sha256(f"{_SECRET_KEY}:{username}:salt".encode()).digest()


def _derive_pepper() -> str:
    """Derive a pepper from the secret key."""
    return sha256(f"{_SECRET_KEY}:pepper".encode()).hexdigest()


def hash_password(username: str, password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256."""
    peppered = f"{password}:{_derive_pepper()}".encode()
    return pbkdf2_hmac("sha256", peppered, _derive_salt(username), 200_000).hex()


def has_admin_credentials(settings: dict[str, object] | None = None) -> bool:
    """Return whether a username *and* password hash are stored.

    Authentication cannot be switched on without both; the settings API refuses
    the flag otherwise, so an enabled-but-credential-less state should never be
    reachable.
    """
    if settings is None:
        try:
            settings = get_settings(include_internal=True)
        except Exception:
            logger.exception("Unable to load admin credentials")
            return False
    username = str(settings.get("admin_username") or "").strip()
    password_hash = str(settings.get("admin_password_hash") or "").strip()
    return bool(username and password_hash)


def verify_login(username: str, password: str) -> bool:
    """Verify login credentials against the stored admin account."""
    try:
        settings = get_settings(include_internal=True)
    except Exception:
        # A storage failure must never authenticate anyone: there is no
        # fallback credential to fall back to.
        logger.exception("Unable to load authentication settings")
        return False

    stored_user = str(settings.get("admin_username") or "").strip()
    stored_hash = str(settings.get("admin_password_hash") or "").strip()
    if not stored_user or not stored_hash:
        # Authentication is on but no account exists - an inconsistent state
        # the settings API prevents. Fail closed; recovering means clearing
        # enable_authentication in the settings table.
        logger.error(
            "Authentication is enabled but no admin account is stored; login is impossible"
        )
        return False

    # The salt is derived from the username, so the candidate hash has to be
    # computed against the *stored* name, not the submitted one.
    password_ok = hmac.compare_digest(hash_password(stored_user, password), stored_hash)
    # Compared as bytes: compare_digest() rejects str operands containing
    # non-ASCII, so a submitted username like "bäcker" would raise TypeError
    # and surface as a 500 instead of a plain failed login.
    username_ok = hmac.compare_digest(username.encode("utf-8"), stored_user.encode("utf-8"))
    return username_ok and password_ok


def current_user(request: Request) -> str | None:
    """Get current authenticated user from request cookies."""
    if not is_authentication_enabled():
        # No login gate at all: hand back the local principal so every
        # authorization dependency keeps working unchanged.
        return LOCAL_USER
    return validate_session(request.cookies.get(SESSION_COOKIE))


def is_authentication_enabled() -> bool:
    """Return whether the application requires a login.

    Reads the in-memory session-settings cache rather than sqlite directly:
    this is called from ``current_user()``, which async route handlers invoke
    inline (not through ``Depends``) on every HTML page and SSE tick, so a
    blocking database read here would run on the event loop. The cache is
    refreshed synchronously at startup and on every settings write (see
    app/session.py::refresh_session_settings_cache), and periodically in the
    background, so it fails closed by default and never lags a local change
    by more than app/main.py's refresh interval.
    """
    return get_cached_authentication_enabled()


def require_user(request: Request) -> str:
    """Dependency: require authenticated user."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_session(request: Request) -> str:
    """Dependency: require a valid authenticated session."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_user_json(request: Request) -> str:
    """Dependency: require authenticated user (JSON response)."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def require_html_auth(request: Request) -> RedirectResponse | None:
    """Check auth and return redirect if not authenticated."""
    if current_user(request):
        return None
    return RedirectResponse(url="/login", status_code=303)


def _require_templates() -> Jinja2Templates:
    if _templates is None:
        raise RuntimeError("Auth module not initialized. Call init_auth() before using routes.")
    return _templates


def _do_logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    delete_session_cookie(response, request)
    return response


# ============================================================================
# Routes
# ============================================================================


@router.get("/login", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def login_page(request: Request):
    """Login page."""
    if current_user(request):
        return RedirectResponse(url="/", status_code=303)

    templates = _require_templates()
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_login_template_context(
            request,
            captcha_token=issue_captcha_token(_SECRET_KEY),
            honeypot_field=HONEYPOT_FIELD_NAME,
        ),
    )


class LoginRequest(BaseModel):
    """JSON body for login API."""
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    honeypot: str = Field(default="", max_length=1024)
    captcha_token: str = Field(default="", max_length=1024)


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest):
    """Process login (JSON API)."""
    if not is_authentication_enabled():
        # Authentication-disabled mode has no login gate. Keep CAPTCHA and
        # credential verification inactive as well and let the caller proceed
        # as the configured application user via current_user().
        return JSONResponse(content={"ok": True, "redirect": "/"})

    captcha_outcome = verify_captcha_token(
        token=body.captcha_token,
        honeypot=body.honeypot,
        secret_key=_SECRET_KEY,
    )
    if captcha_outcome is not CaptchaOutcome.OK:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": _CAPTCHA_REJECTED_MESSAGE},
        )

    if not await asyncio.to_thread(verify_login, body.username, body.password):
        return JSONResponse(
            status_code=401,
            content={"ok": False, "detail": "Invalid credentials"},
        )

    token = create_session(body.username)
    response = JSONResponse(content={"ok": True, "redirect": "/"})
    set_session_cookie(response, token, request)
    return response


@router.post("/logout")
@limiter.limit("20/minute")
async def logout_post(request: Request):
    """Process logout (POST)."""
    return _do_logout(request)
