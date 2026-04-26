#!/usr/bin/env python3
#
# middleware/csrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CSRF middleware and helpers for tubeyou login/session routes."""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def generate_csrf_token(secret_key: str, session_nonce: str) -> str:
    """Generate deterministic CSRF token bound to secret key and session nonce."""
    key_bytes = secret_key.encode("utf-8")
    msg_bytes = f"csrf:{session_nonce}".encode("utf-8")
    return hmac.new(key_bytes, msg_bytes, sha256).hexdigest()


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF middleware for selected state-changing routes."""

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        *,
        csrf_cookie_name: str = "tubeyou_csrf",
        protected_paths: tuple[str, ...] = ("/login", "/logout", "/api/submit"),
    ) -> None:
        super().__init__(app)
        self._secret_key = secret_key
        self._csrf_cookie_name = csrf_cookie_name
        self._protected_paths = protected_paths

    def _path_protected(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self._protected_paths)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        csrf_cookie = request.cookies.get(self._csrf_cookie_name)
        new_cookie = False

        if not csrf_cookie:
            nonce = secrets.token_urlsafe(24)
            csrf_cookie = generate_csrf_token(self._secret_key, nonce)
            new_cookie = True

        request.state.csrf_token = csrf_cookie

        if request.method not in SAFE_METHODS and self._path_protected(request.url.path):
            sent_token = request.headers.get("X-CSRF-Token")
            if not sent_token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Missing CSRF token"},
                )
            if not secrets.compare_digest(csrf_cookie, sent_token):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid CSRF token"},
                )

        response = await call_next(request)

        if new_cookie:
            response.set_cookie(
                key=self._csrf_cookie_name,
                value=csrf_cookie,
                httponly=False,
                secure=request.url.scheme == "https",
                samesite="lax",
                max_age=3600,
            )

        return response
