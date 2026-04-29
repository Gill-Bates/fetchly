#!/usr/bin/env python3
#
# app/routes/api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Core API routes for jobs, settings, and info."""

import asyncio
import logging
from queue import Full
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Protocol
from weakref import WeakKeyDictionary

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..common.rate_limit import limiter
from ..bpm_cluster import cluster_bpms
from ..db import (
    get_job,
    get_settings,
    get_stats,
    insert_job,
    list_completed_bpms,
    paginate_jobs,
    set_settings,
    update_job,
    update_job_if_status,
)
from ..session import delete_session_cookie, refresh_session_settings_cache
from ..utils.template_filters import is_lalala_configured, public_settings
from ..utils.youtube import (
    empty_info_payload,
    extract_video_meta_async,
    load_video_info_async,
    normalize_info_url,
    validate_youtube_url,
)
from ..worker import cancel_job as cancel_worker_job, get_job_queue
from .auth import (
    hash_password,
    require_html_auth,
    require_session,
    require_user,
    require_user_json,
    verify_login,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["api"])

# Constants
_ALLOWED_MEDIA_TYPES = frozenset({"audio", "video"})
_ALLOWED_QUALITIES = frozenset({"max", "medium", "small"})
_STATS_CACHE_TTL_SECONDS = 60.0
_TERMINAL_JOB_STATUSES = frozenset({"done", "analysis", "analysis_done", "error", "cancelled"})
_PERSISTED_CANCELLABLE_JOB_STATUSES = ("queued", "processing")

# Module-level state
_templates: "Jinja2Templates | None" = None
_DEFAULT_USER: str = ""
_stats_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_stats_locks: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = WeakKeyDictionary()


def init_api(
    templates: "Jinja2Templates",
    default_user: str,
) -> None:
    """Initialize the API module with required dependencies."""
    global _templates, _DEFAULT_USER
    _templates = templates
    _DEFAULT_USER = default_user


async def _get_json(request: Request) -> dict[str, Any]:
    """Parse request body as JSON and ensure it is a dict."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return payload


def _require_templates() -> "Jinja2Templates":
    if _templates is None:
        raise RuntimeError("API module not initialized: call init_api() first")
    return _templates


def _get_stats_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _stats_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _stats_locks[loop] = lock
    return lock


def _clamp_int(value: Any, min_value: int, max_value: int, name: str) -> int:
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from exc
    if not (min_value <= int_value <= max_value):
        raise HTTPException(status_code=400, detail=f"{name} must be between {min_value} and {max_value}")
    return int_value


class JobRecord(Protocol):
    def __getitem__(self, key: str) -> Any: ...


def job_to_dict(job: JobRecord) -> dict[str, object]:
    """Convert a job record to a dictionary for JSON responses."""
    return {
        "id": job["id"],
        "url": job["url"],
        "video_title": job["video_title"],
        "video_meta_hover": job["video_meta_hover"],
        "type": job["type"],
        "quality": job["quality"],
        "status": job["status"],
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "message": job["message"],
        "filesize_bytes": job["filesize_bytes"],
        "duration_seconds": job["duration_seconds"],
        "codec": job["codec"],
        "bitrate_kbps": job["bitrate_kbps"],
        "bpm": job["bpm"],
        "bpm_confidence": job["bpm_confidence"],
        "audio_hash": job["audio_hash"],
        "filename": job["filename"],
    }


async def get_cached_stats() -> dict[str, int]:
    """Return dashboard stats from a short TTL cache to avoid repeated scans."""
    now_ts = time()
    cached = _stats_cache.get("data")
    cached_ts = float(_stats_cache.get("ts", 0.0) or 0.0)
    if cached is not None and (now_ts - cached_ts) < _STATS_CACHE_TTL_SECONDS:
        return cached

    async with _get_stats_lock():
        now_ts = time()
        cached = _stats_cache.get("data")
        cached_ts = float(_stats_cache.get("ts", 0.0) or 0.0)
        if cached is not None and (now_ts - cached_ts) < _STATS_CACHE_TTL_SECONDS:
            return cached

        stats = await asyncio.to_thread(get_stats)
        _stats_cache["data"] = stats
        _stats_cache["ts"] = time()
        return stats


# ============================================================================
# Routes
# ============================================================================


@router.get("/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/api/stats")
@limiter.limit("30/minute")
async def api_stats(request: Request, _: str = Depends(require_user_json)) -> dict[str, int]:
    """Return fresh dashboard stats for live UI updates."""
    _ = request
    return await asyncio.to_thread(get_stats)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Dashboard home page."""
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    templates = _require_templates()

    jobs = await asyncio.to_thread(paginate_jobs, limit=50, offset=0)
    stats = await get_cached_stats()
    settings = await asyncio.to_thread(get_settings)
    lalal_enabled = is_lalala_configured(settings)
    return templates.TemplateResponse(request=request, name="index.html", context={
        "jobs": jobs,
        "stats": stats,
        "lalal_enabled": lalal_enabled,
        "csrf_token": getattr(request.state, "csrf_token", ""),
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    templates = _require_templates()

    settings = await asyncio.to_thread(get_settings)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": public_settings(settings),
            "csrf_token": getattr(request.state, "csrf_token", ""),
        },
    )


@router.get("/api/jobs")
@limiter.limit("60/minute")
async def api_jobs(request: Request, _user: str = Depends(require_user), offset: int = 0, limit: int = 50):
    """List jobs with pagination."""
    _ = request

    safe_offset = max(0, offset)
    safe_limit = min(max(1, limit), 100)

    jobs = await asyncio.to_thread(paginate_jobs, limit=safe_limit, offset=safe_offset)
    return [job_to_dict(job) for job in jobs]


@router.get("/api/stats/bpm-clusters")
@limiter.limit("30/minute")
async def api_bpm_clusters(request: Request, _user: str = Depends(require_user), limit: int = 1000):
    """Get BPM clusters for visualization."""
    _ = request
    safe_limit = min(max(1, limit), 5000)
    bpms = await asyncio.to_thread(list_completed_bpms, safe_limit)
    clusters = await asyncio.to_thread(cluster_bpms, bpms)
    return [
        {"bpm": bpm_bucket, "count": count}
        for bpm_bucket, count in clusters
    ]


@router.get("/api/info")
@limiter.limit("20/minute")
async def api_info(request: Request, url: str, user: str = Depends(require_user)):
    """Extract video metadata using yt-dlp."""
    _ = request
    is_valid, error_msg = validate_youtube_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    try:
        info_url = normalize_info_url(url)
        info = await asyncio.wait_for(load_video_info_async(info_url), timeout=20.0)

        if not info:
            return empty_info_payload()

        title = str(info.get("title") or "").strip()
        channel = str(info.get("channel") or "").strip()
        uploader = str(info.get("uploader") or "").strip()
        duration = info.get("duration")
        views = info.get("view_count")

        formats: list[dict[str, Any]] = []
        raw_formats = info.get("formats") or []
        seen: set[str] = set()
        for fmt in raw_formats:
            if not isinstance(fmt, dict):
                continue
            ext = fmt.get("ext", "")
            vcodec = fmt.get("vcodec", "")
            acodec = fmt.get("acodec", "")
            height = fmt.get("height")
            abr = fmt.get("abr")

            if vcodec and vcodec != "none":
                label = f"{height}p" if height else "video"
            elif acodec and acodec != "none":
                label = f"{abr}kbps" if abr else "audio"
            else:
                continue

            key = f"{label}-{ext}"
            if key in seen:
                continue
            seen.add(key)

            formats.append(
                {
                    "format_id": fmt.get("format_id"),
                    "ext": ext,
                    "resolution": label,
                    "filesize": fmt.get("filesize"),
                }
            )

        return {
            "title": title or None,
            "channel": channel or None,
            "uploader": uploader or None,
            "duration": duration if isinstance(duration, int) else None,
            "view_count": views if isinstance(views, int) else None,
            "formats": formats[:20],
            "formats_total": len(formats),
            "formats_truncated": len(formats) > 20,
            "unavailable": False,
        }

    except asyncio.TimeoutError:
        logger.warning("Video info extraction timed out for URL %s", url)
        return empty_info_payload()
    except OSError as exc:
        logger.exception("System error during video info extraction for URL %s", url)
        raise HTTPException(status_code=503, detail="Metadata extraction temporarily unavailable") from exc
    except Exception as exc:
        logger.warning("Video info extraction failed for URL %s: %s", url, exc, exc_info=True)
        return empty_info_payload()


@router.get("/api/settings")
@limiter.limit("60/minute")
async def api_get_settings(request: Request, _user: str = Depends(require_user)):
    """Get all settings."""
    _ = request
    settings = await asyncio.to_thread(get_settings)
    return public_settings(settings)


@router.post("/api/settings")
@limiter.limit("5/minute")
async def api_set_settings(request: Request, _user: str = Depends(require_session)):
    """Update retention/session settings and optional admin password."""
    payload = await _get_json(request)

    settings_to_update: dict[str, Any] = {}
    password_changed = False

    if "retention_days" in payload:
        settings_to_update["retention_days"] = _clamp_int(payload["retention_days"], 1, 365, "retention_days")

    if "session_idle_minutes" in payload:
        settings_to_update["session_idle_minutes"] = _clamp_int(payload["session_idle_minutes"], 5, 1440, "session_idle_minutes")

    try:
        if "admin_password" in payload and payload["admin_password"]:
            new_password = str(payload["admin_password"]).strip()
            if len(new_password) < 8:
                raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

            current_password = str(payload.get("current_password", ""))
            is_valid_current_password = await asyncio.to_thread(verify_login, _DEFAULT_USER, current_password)
            if not is_valid_current_password:
                raise HTTPException(status_code=403, detail="Current password is invalid")

            current_settings = await asyncio.to_thread(get_settings)
            current_session_version = int(current_settings.get("session_version", 0) or 0)

            settings_to_update["admin_password_hash"] = await asyncio.to_thread(
                hash_password,
                _DEFAULT_USER,
                new_password,
            )
            settings_to_update["session_version"] = current_session_version + 1
            password_changed = True

        if settings_to_update:
            await asyncio.to_thread(set_settings, settings_to_update, allow_internal=True)
            await asyncio.to_thread(refresh_session_settings_cache)

        if password_changed:
            response = JSONResponse(
                content={
                    "ok": True,
                    "message": "Password updated. Please log in again.",
                    "redirect": "/login",
                }
            )
            delete_session_cookie(response, request)
            return response

        return {"ok": True, "message": "Settings updated"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Settings update failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/submit")
@limiter.limit("10/minute")
async def api_submit(
    request: Request,
    url: str = Form(...),
    media_type: str = Form(..., alias="type"),
    quality: str = Form(...),
    _user: str = Depends(require_user),
):
    """Submit a new download job."""
    _ = request
    media_type = str(media_type).strip().lower()
    quality_value = str(quality).strip().lower()

    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Allowed: {', '.join(sorted(_ALLOWED_MEDIA_TYPES))}")
    if quality_value not in _ALLOWED_QUALITIES:
        raise HTTPException(status_code=400, detail=f"Invalid quality. Allowed: {', '.join(sorted(_ALLOWED_QUALITIES))}")

    # Validate YouTube URL
    is_valid, error_msg = validate_youtube_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # Try to extract metadata with short timeout - don't block job creation if it fails
    meta: dict[str, object] = {"video_title": None, "video_meta_hover": None}
    try:
        meta = await asyncio.wait_for(extract_video_meta_async(url.strip()), timeout=8.0)
    except Exception as exc:
        logger.debug("Metadata extraction skipped for submit (will be fetched by worker): %s", exc)

    job_id = str(uuid.uuid4())
    clean_url = url.strip()
    job_queue = get_job_queue()
    if job_queue.full():
        raise HTTPException(status_code=503, detail="Job queue is full, please try again later")

    await asyncio.to_thread(
        insert_job,
        job_id,
        clean_url,
        media_type,
        quality_value,
        "queued",
        video_title=meta["video_title"],
        video_meta_hover=meta["video_meta_hover"],
    )
    try:
        job_queue.put_nowait((job_id, clean_url, media_type, quality_value))
    except Full as exc:
        try:
            await asyncio.to_thread(
                update_job,
                job_id,
                status="error",
                message="Queue full at submission time",
                finished_at=datetime.now(UTC).isoformat(),
            )
        except Exception:
            logger.exception("Failed to mark job %s as errored after queue overflow", job_id)
        raise HTTPException(status_code=503, detail="Job queue is full, please try again later") from exc
    job = await asyncio.to_thread(get_job, job_id)
    return job_to_dict(job)


@router.post("/api/jobs/{job_id}/cancel")
@limiter.limit("30/minute")
async def cancel_job(request: Request, job_id: uuid.UUID, _user: str = Depends(require_user_json)):
    """Cancel a running or queued job."""
    _ = request

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    status = job["status"] or ""
    if status in _TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot cancel job with status: {status}")

    # Mark job for cancellation
    cancel_worker_job(job_id_str)

    cancelled = await asyncio.to_thread(
        update_job_if_status,
        job_id_str,
        _PERSISTED_CANCELLABLE_JOB_STATUSES,
        status="cancelled",
        message="Cancelled by user",
        finished_at=datetime.now(UTC).isoformat(),
    )

    if cancelled:
        logger.info("Cancellation requested for job %s (status: %s)", job_id_str, status)
    else:
        logger.info("Cancellation request for job %s lost race to a later status transition", job_id_str)

    job = await asyncio.to_thread(get_job, job_id_str)
    return job_to_dict(job)
