#!/usr/bin/env python3
#
# app/routes/auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authentication routes and helpers."""

from __future__ import annotations

import hmac
import logging
import os
from hashlib import pbkdf2_hmac, sha256
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ..db import get_settings
from ..common.rate_limit import limiter
from ..session import (
    SESSION_COOKIE,
    create_session,
    validate_session,
    set_session_cookie,
    delete_session_cookie,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level state (set during init)
_templates: "Jinja2Templates | None" = None
_SECRET_KEY: str = ""
_DEFAULT_USER: str = ""
_DEFAULT_HASH: str = ""


def _login_template_context(request: Request, **extra: object) -> dict[str, object]:
    """Build template context with CSRF token for login page."""
    context: dict[str, object] = {"csrf_token": getattr(request.state, "csrf_token", "")}
    context.update(extra)
    return context


def init_auth(
    templates: "Jinja2Templates",
    secret_key: str,
    default_user: str,
    default_password: str,
) -> None:
    """Initialize the auth module with required dependencies.
    
    Must be called before routes are used.
    """
    global _templates, _SECRET_KEY, _DEFAULT_USER, _DEFAULT_HASH
    _templates = templates
    _SECRET_KEY = secret_key
    _DEFAULT_USER = default_user
    _DEFAULT_HASH = hash_password(default_user, default_password)


def _derive_salt(username: str) -> bytes:
    """Derive a salt from the secret key and username."""
    return sha256(f"{_SECRET_KEY}:{username}:salt".encode("utf-8")).digest()


def _derive_pepper() -> str:
    """Derive a pepper from the secret key."""
    return sha256(f"{_SECRET_KEY}:pepper".encode("utf-8")).hexdigest()


def hash_password(username: str, password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256."""
    peppered = f"{password}:{_derive_pepper()}".encode("utf-8")
    return pbkdf2_hmac("sha256", peppered, _derive_salt(username), 200_000).hex()


def verify_login(username: str, password: str) -> bool:
    """Verify login credentials against stored hash."""
    stored_hash = _DEFAULT_HASH

    # Check if a custom password hash is stored in settings.
    try:
        settings = get_settings()
        custom_hash = settings.get("admin_password_hash")
        if custom_hash and str(custom_hash).strip():
            stored_hash = str(custom_hash).strip()
    except Exception:
        logger.warning("Could not load password hash from settings; falling back to default", exc_info=True)

    password_ok = hmac.compare_digest(hash_password(_DEFAULT_USER, password), stored_hash)
    username_ok = hmac.compare_digest(username, _DEFAULT_USER)
    return username_ok and password_ok


def current_user(request: Request) -> str | None:
    """Get current authenticated user from request cookies."""
    return validate_session(request.cookies.get(SESSION_COOKIE))


def require_user(request: Request) -> str:
    """Dependency: require authenticated user."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_session(request: Request) -> str:
    """Dependency: require a valid authenticated session."""
    user = validate_session(request.cookies.get(SESSION_COOKIE))
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


def _require_templates() -> "Jinja2Templates":
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
        context=_login_template_context(request),
    )


class LoginRequest(BaseModel):
    """JSON body for login API."""
    username: str
    password: str


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest):
    """Process login (JSON API)."""
    if not verify_login(body.username, body.password):
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
