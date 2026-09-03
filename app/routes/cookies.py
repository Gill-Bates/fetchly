#!/usr/bin/env python3
#
# app/routes/cookies.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Import and validity checks for platform cookie files.

Cookies are the only way fetchly can download age-/login-gated content from
YouTube, TikTok, Instagram and Facebook (see app/utils/platform.py and
app/utils/cookies.py). Settings offers one way in - a paste of whatever the
browser's dev tools produce, converted by app/utils/cookie_import.py - while
the file upload below stays for scripted setups that already hold a prepared
jar. Both end up in the same place, through the same conversion and the same
structural check from app/utils/cookie_status.py.

The check is structural only - it never talks to the platform itself, which
would need an actual API/page request per cookie file and would risk tripping
the platform's own abuse detection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..common.rate_limit import limiter
from ..utils.cookie_import import (
    CookieImport,
    CookieImportError,
    convert_to_netscape,
    has_session_cookie,
    missing_session_cookie_hint,
)
from ..utils.cookie_status import CookieAnalysis, analyze_cookie_file
from ..utils.cookies import default_cookie_file
from ..utils.platform import PLATFORM_COOKIE_FILENAMES
from .auth import require_session, require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cookies", tags=["cookies"])

# A real Netscape cookie export is a few KB even with dozens of cookies. This
# bound is generous headroom while still refusing anything that is clearly not
# a cookie file (e.g. an accidentally selected video/archive).
_MAX_COOKIE_FILE_BYTES: Final = 256 * 1024


class CookiePaste(BaseModel):
    """Whatever the user copied out of the browser's dev tools.

    ``max_length`` counts characters, so it alone would let a paste of
    multi-byte characters through at several times the intended size; the
    endpoint bounds the encoded length as well.
    """

    text: str = Field(min_length=1, max_length=_MAX_COOKIE_FILE_BYTES)


class CookieStatus(BaseModel):
    """Validity snapshot for one platform's cookie file."""

    platform: str
    filename: str
    present: bool
    status: str  # "valid" | "expired" | "invalid" | "missing"
    authenticated: bool = False
    cookie_count: int = 0
    matching_domain_count: int = 0
    expires_at: int | None = None
    updated_at: int | None = None
    domains: list[str] = []
    missing_login_cookies: list[str] = []
    detail: str = ""


def _status_from_analysis(analysis: CookieAnalysis) -> CookieStatus:
    """Wrap the shared structural check in this module's API contract."""
    return CookieStatus(
        filename=PLATFORM_COOKIE_FILENAMES.get(analysis.platform, ""),
        platform=analysis.platform,
        present=analysis.present,
        status=analysis.status,
        authenticated=analysis.is_authenticated,
        cookie_count=analysis.cookie_count,
        matching_domain_count=analysis.matching_domain_count,
        expires_at=analysis.expires_at,
        updated_at=analysis.updated_at,
        domains=list(analysis.domains),
        missing_login_cookies=list(analysis.missing_login_cookies),
        detail=analysis.detail,
    )


def _filename_for(platform: str) -> str:
    filename = PLATFORM_COOKIE_FILENAMES.get(platform)
    if not filename:
        raise HTTPException(status_code=404, detail="Unknown platform")
    return filename


def _resolve_cookie_path(platform: str) -> Path:
    filename = _filename_for(platform)
    return default_cookie_file(filename)


def _convert(platform: str, text: str, *, require_login: bool) -> CookieImport:
    """Normalize pasted or uploaded cookie text into a Netscape jar.

    ``require_login`` guards the paste box only. A paste without any of the
    platform's HttpOnly session cookies is almost always the same mistake -
    ``document.cookie`` typed into the console, which cannot see them - and
    storing that jar would leave downloads signed out while Settings claims
    cookies are in place. An uploaded file keeps the old, permissive contract:
    exports differ, and an operator who prepared a file on purpose is not
    guessing.

    This is the cheap check, on names alone, so an obvious mistake is refused
    before anything is written. _validate_and_store repeats it against the
    parsed jar, where cookie domains are known and a foreign cookie sharing a
    name cannot pass for the platform's own.
    """
    try:
        imported = convert_to_netscape(text, platform)
    except CookieImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if require_login and not has_session_cookie(imported.names, platform):
        raise HTTPException(status_code=400, detail=missing_session_cookie_hint(platform))

    return imported


def _validate_and_store(platform: str, text: str, *, require_login: bool) -> CookieStatus:
    """Validate converted cookie text, then publish it atomically.

    The candidate is written to a scratch file in the *target directory* and
    only renamed into place once it parses. Two reasons it may not be written
    to the live path directly: a reader would otherwise see a half-written
    jar - up to eight worker threads share this file, and yt-dlp reads it at
    the start of every job - and a truncate-in-place would leave nothing at
    all behind if the process died mid-write. os.replace() makes the swap
    atomic, so a reader sees either the old jar or the new one.

    Publishing a fresh file also repairs the permissions of an existing jar:
    O_CREAT's mode only applies when the file did not exist, so overwriting a
    world-readable file left it world-readable.
    """
    filename = _filename_for(platform)
    target = default_cookie_file(filename)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Same directory as the target: os.replace() is only atomic within one
    # filesystem. mkstemp creates at 0600; fchmod states it independently of
    # any future change to that default, because this file holds a live login.
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".cookies-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    published = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(text)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        analysis = analyze_cookie_file(tmp_path, platform)
        if analysis.status == "invalid":
            raise HTTPException(status_code=400, detail=analysis.detail or "Invalid cookie file")

        # Re-checked against the parsed jar rather than the names the importer
        # collected: this test is domain-aware, so a cookie named "sessionid"
        # belonging to some other site cannot satisfy it.
        if require_login and analysis.missing_login_cookies:
            raise HTTPException(status_code=400, detail=missing_session_cookie_hint(platform))

        tmp_path.replace(target)
        published = True

        return _status_from_analysis(analyze_cookie_file(target, platform))
    finally:
        if not published:
            tmp_path.unlink(missing_ok=True)


def _delete_cookie_files(platform: str) -> bool:
    filename = _filename_for(platform)
    target = default_cookie_file(filename)
    if not target.is_file():
        return False
    target.unlink(missing_ok=True)
    return True


# ============================================================================
# Routes
# ============================================================================


async def list_cookie_statuses() -> list[CookieStatus]:
    """Return the validity snapshot for every supported platform's cookie file.

    Shared by the JSON status endpoint below and the initial Settings page
    render, so both start from the same on-disk check.
    """
    results: list[CookieStatus] = []
    for platform in PLATFORM_COOKIE_FILENAMES:
        path = await asyncio.to_thread(_resolve_cookie_path, platform)
        analysis = await asyncio.to_thread(analyze_cookie_file, path, platform)
        results.append(_status_from_analysis(analysis))
    return results


@router.get("", response_model=list[CookieStatus])
@limiter.limit("30/minute")
async def api_cookies_status(
    request: Request, _user: str = Depends(require_user)
) -> list[CookieStatus]:
    """Return the validity snapshot for every supported platform's cookie file."""
    _ = request
    return await list_cookie_statuses()


@router.post("/{platform}", response_model=CookieStatus)
@limiter.limit("10/minute")
async def api_cookies_upload(
    request: Request,
    platform: str,
    file: UploadFile = File(...),
    _user: str = Depends(require_session),
) -> CookieStatus:
    """Upload a cookie file for one platform.

    Netscape files and the JSON exports of cookie extensions are both
    accepted; app/utils/cookie_import.py reduces either to the jar yt-dlp
    reads.
    """
    _ = request
    _filename_for(platform)  # 404s early for an unknown platform

    body = await file.read(_MAX_COOKIE_FILE_BYTES + 1)
    if len(body) > _MAX_COOKIE_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Cookie file is too large")
    if not body:
        raise HTTPException(status_code=400, detail="Cookie file is empty")

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Cookie file must be UTF-8 text") from exc

    imported = await asyncio.to_thread(_convert, platform, text, require_login=False)
    return await asyncio.to_thread(
        _validate_and_store, platform, imported.netscape, require_login=False
    )


@router.post("/{platform}/paste", response_model=CookieStatus)
@limiter.limit("10/minute")
async def api_cookies_paste(
    request: Request,
    platform: str,
    payload: CookiePaste,
    _user: str = Depends(require_session),
) -> CookieStatus:
    """Store cookies pasted from the browser's dev tools for one platform."""
    _ = request
    _filename_for(platform)  # 404s early for an unknown platform

    if len(payload.text.encode("utf-8")) > _MAX_COOKIE_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Pasted cookies are too large")

    imported = await asyncio.to_thread(_convert, platform, payload.text, require_login=True)
    status = await asyncio.to_thread(
        _validate_and_store, platform, imported.netscape, require_login=True
    )
    logger.info(
        "Imported %d cookies for %s from a pasted %s",
        imported.cookie_count,
        platform,
        imported.source_format,
    )
    return status


@router.delete("/{platform}")
@limiter.limit("10/minute")
async def api_cookies_delete(
    request: Request, platform: str, _user: str = Depends(require_session)
) -> dict[str, Any]:
    """Remove a platform's stored cookie file, if any."""
    _ = request
    _filename_for(platform)  # 404s early for an unknown platform

    removed = await asyncio.to_thread(_delete_cookie_files, platform)
    if not removed:
        raise HTTPException(status_code=404, detail="No cookie file to remove")
    return {"ok": True, "message": "Cookie file removed"}
