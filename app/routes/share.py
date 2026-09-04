#!/usr/bin/env python3
#
# app/routes/share.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Public share-link routes.

A share link is an unguessable token that grants access to one job's output
file without a session. Creating a link requires authentication; redeeming it
does not, so the redeem route carries its own rate limit and never reveals
whether a token merely does not exist, is exhausted, or points at artifacts
that housekeeping has already removed - every one of those is a plain 404.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..common.rate_limit import limiter
from ..db import consume_share_link, create_share_link, get_settings, get_share_link
from ..utils.public_url import build_public_base_url
from .auth import require_user_json
from .media import build_job_file_response

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share"])

_templates: Jinja2Templates | None = None


def init_share(templates: Jinja2Templates) -> None:
    global _templates
    _templates = templates


def _unavailable(request: Request) -> Response:
    """Render the public "link unavailable" page (one page for every failure
    mode, so an anonymous visitor cannot tell whether a token exists).
    """
    if _templates is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _templates.TemplateResponse(
        request=request,
        name="share_error.html",
        context={},
        status_code=404,
    )

# Tokens are 8 URL-safe characters (see create_share_link). The upper bound
# stays generous so lengthening tokens later does not silently 404 every link.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _share_url(request: Request, token: str, public_hostname: str = "") -> str:
    base = build_public_base_url(request, public_hostname)
    return f"{base}/share/{token}"


@router.post("/api/share/{job_id}")
@limiter.limit("20/minute")
async def create_share(
    request: Request,
    job_id: uuid.UUID,
    _user: str = Depends(require_user_json),
) -> Response:
    """Create (or reuse) a share link for a finished job."""
    # Resolve the file first: a job that is not downloadable gets no link.
    probe = await build_job_file_response(job_id)
    if isinstance(probe, JSONResponse):
        return probe

    settings = await asyncio.to_thread(get_settings)
    try:
        max_uses = int(settings.get("share_link_max_uses", 0) or 0)
    except (TypeError, ValueError):
        max_uses = 0
    max_uses = max(0, max_uses)

    public_hostname = str(settings.get("public_hostname", "") or "")

    token = await asyncio.to_thread(create_share_link, str(job_id), max_uses)
    logger.info("Created share link for job %s (max_uses=%d)", job_id, max_uses)

    return JSONResponse(
        content={
            "ok": True,
            "url": _share_url(request, token, public_hostname),
            "max_uses": max_uses,
        }
    )


@router.get("/share/{token}")
@limiter.limit("20/minute")
async def redeem_share(request: Request, token: str) -> Response:
    """Serve a shared job file. Public: no session required."""
    if not _TOKEN_RE.fullmatch(token):
        return _unavailable(request)

    link = await asyncio.to_thread(get_share_link, token)
    if link is None:
        return _unavailable(request)

    try:
        job_id = uuid.UUID(str(link["job_id"]))
    except ValueError:
        return _unavailable(request)

    # Check the artifacts before counting a use: once housekeeping has removed
    # the job directory the link is dead anyway, and a 404 must not consume
    # quota from a link that might still be valid for a re-downloaded job.
    response = await build_job_file_response(job_id)
    if isinstance(response, JSONResponse):
        return _unavailable(request)

    if not await asyncio.to_thread(consume_share_link, token):
        return _unavailable(request)

    return response
