#!/usr/bin/env python3
#
# middleware/csrf.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CSRF middleware and helpers for tubeyou login/session routes."""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Characters that must not appear in a cookie name per RFC 6265.
_BAD_COOKIE_NAME_CHARS = re.compile(r'[;=\s]')


def generate_csrf_token() -> str:
    """Return a random token suitable for double-submit cookie CSRF protection."""
    return secrets.token_urlsafe(32)


class CSRFMiddleware:
    """Double-submit cookie CSRF middleware for selected state-changing routes."""

    _COOKIE_MAX_AGE = 3600
    _COOKIE_SAMESITE = "lax"
    _COOKIE_PATH = "/"

    def __init__(
        self,
        app: ASGIApp,
        *,
        csrf_cookie_name: str = "tubeyou_csrf",
        protected_paths: tuple[str, ...] = ("/login", "/logout", "/api/submit"),
    ) -> None:
        self.app = app
        self._csrf_cookie_name = csrf_cookie_name
        self._protected_paths = protected_paths

        if _BAD_COOKIE_NAME_CHARS.search(csrf_cookie_name):
            raise ValueError("csrf_cookie_name contains illegal characters")

        for p in protected_paths:
            if not p.startswith("/"):
                raise ValueError(f"protected_paths must start with '/': {p}")

    def _path_protected(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self._protected_paths)

    async def _reject(
        self,
        detail: str,
        *,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
        set_cookie_value: str | None = None,
        secure: bool = False,
    ) -> None:
        """Return a 403 JSON response, optionally setting the CSRF cookie."""
        response = JSONResponse(status_code=403, content={"detail": detail})
        if set_cookie_value is not None:
            response.set_cookie(
                key=self._csrf_cookie_name,
                value=set_cookie_value,
                path=self._COOKIE_PATH,
                max_age=self._COOKIE_MAX_AGE,
                httponly=False,
                secure=secure,
                samesite=self._COOKIE_SAMESITE,  # type: ignore[arg-type]
            )
        await response(scope, receive, send)

    def _cookie_header(self, value: str, *, secure: bool) -> str:
        """Serialize the CSRF cookie for direct header injection."""
        parts = [
            f"{self._csrf_cookie_name}={value}",
            f"Max-Age={self._COOKIE_MAX_AGE}",
            f"Path={self._COOKIE_PATH}",
            f"SameSite={self._COOKIE_SAMESITE.capitalize()}",
        ]
        if secure:
            parts.append("Secure")
        # Intentionally omit HttpOnly so JavaScript can read the token.
        return "; ".join(parts)

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        csrf_cookie = request.cookies.get(self._csrf_cookie_name)
        is_new_cookie = False

        if not csrf_cookie:
            csrf_cookie = generate_csrf_token()
            is_new_cookie = True

        # Expose the current token to downstream handlers (e.g., for HTML meta tags).
        scope.setdefault("state", {})["csrf_token"] = csrf_cookie

        method = request.method
        path = request.url.path
        secure = request.url.scheme == "https"

        # Track if we've consumed the body (need to replay for downstream)
        body_cache: bytes | None = None
        body_consumed = False

        if method not in SAFE_METHODS and self._path_protected(path):
            # Check header first (for JS fetch requests)
            sent_token = request.headers.get("X-CSRF-Token")
            
            # Fall back to form data for traditional HTML form submissions.
            if not sent_token:
                content_type = request.headers.get("content-type", "")
                if content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data"):
                    try:
                        form_data = await request.form()
                        sent_token = form_data.get("csrf_token")
                        body_consumed = True
                    except Exception:
                        try:
                            body_cache = await request.body()
                            body_consumed = True
                            from urllib.parse import parse_qs
                            form_data = parse_qs(body_cache.decode("utf-8"))
                            sent_token = form_data.get("csrf_token", [None])[0]
                        except Exception:
                            pass
            
            if not sent_token:
                await self._reject(
                    "Missing CSRF token",
                    scope=scope,
                    receive=receive,
                    send=send,
                    set_cookie_value=csrf_cookie if is_new_cookie else None,
                    secure=secure,
                )
                return

            if not secrets.compare_digest(csrf_cookie, sent_token):
                await self._reject(
                    "Invalid CSRF token",
                    scope=scope,
                    receive=receive,
                    send=send,
                    set_cookie_value=csrf_cookie if is_new_cookie else None,
                    secure=secure,
                )
                return

        # If we consumed the body, create a receive that replays it
        if body_consumed and body_cache is not None:
            body_sent = False
            
            async def receive_with_body() -> dict:
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body_cache, "more_body": False}
                return {"type": "http.disconnect"}
            
            receive = receive_with_body

        async def send_wrapper(message: dict) -> None:
            if is_new_cookie and message.get("type") == "http.response.start":
                status = message.get("status", 200)
                if status not in (204, 304, 205):
                    headers = MutableHeaders(scope=message)
                    headers.append(
                        "set-cookie",
                        self._cookie_header(csrf_cookie, secure=secure),
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
