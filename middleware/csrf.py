#!/usr/bin/env python3
#
# middleware/csrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CSRF middleware and helpers for tubeyou login/session routes."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable
from hashlib import sha256

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def generate_csrf_token(secret_key: str, session_nonce: str) -> str:
    """Generate deterministic CSRF token bound to secret key and session nonce."""
    key_bytes = secret_key.encode("utf-8")
    msg_bytes = f"csrf:{session_nonce}".encode("utf-8")
    return hmac.new(key_bytes, msg_bytes, sha256).hexdigest()


class CSRFMiddleware:
    """Double-submit CSRF middleware for selected state-changing routes."""

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        *,
        csrf_cookie_name: str = "tubeyou_csrf",
        protected_paths: tuple[str, ...] = ("/login", "/logout", "/api/submit"),
    ) -> None:
        self.app = app
        self._secret_key = secret_key
        self._csrf_cookie_name = csrf_cookie_name
        self._protected_paths = protected_paths

    def _path_protected(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self._protected_paths)

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        csrf_cookie = request.cookies.get(self._csrf_cookie_name)
        new_cookie = False

        if not csrf_cookie:
            nonce = secrets.token_urlsafe(24)
            csrf_cookie = generate_csrf_token(self._secret_key, nonce)
            new_cookie = True

        scope.setdefault("state", {})["csrf_token"] = csrf_cookie

        if request.method not in SAFE_METHODS and self._path_protected(request.url.path):
            sent_token = request.headers.get("X-CSRF-Token")
            if not sent_token:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Missing CSRF token"},
                )
                if new_cookie:
                    response.set_cookie(
                        key=self._csrf_cookie_name,
                        value=csrf_cookie,
                        httponly=False,
                        secure=request.url.scheme == "https",
                        samesite="lax",
                        max_age=3600,
                    )
                await response(scope, receive, send)
                return
            if not secrets.compare_digest(csrf_cookie, sent_token):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid CSRF token"},
                )
                if new_cookie:
                    response.set_cookie(
                        key=self._csrf_cookie_name,
                        value=csrf_cookie,
                        httponly=False,
                        secure=request.url.scheme == "https",
                        samesite="lax",
                        max_age=3600,
                    )
                await response(scope, receive, send)
                return

        async def send_wrapper(message: dict) -> None:
            if new_cookie and message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                temp = Response()
                temp.set_cookie(
                    key=self._csrf_cookie_name,
                    value=csrf_cookie,
                    httponly=False,
                    secure=request.url.scheme == "https",
                    samesite="lax",
                    max_age=3600,
                )
                for name, value in temp.raw_headers:
                    if name == b"set-cookie":
                        headers.append("set-cookie", value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_wrapper)
