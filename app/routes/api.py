#!/usr/bin/env python3
#
# app/routes/api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Core API routes for jobs, settings, and info."""

import asyncio
import hashlib
import logging
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote
from weakref import WeakKeyDictionary

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..bpm_cluster import cluster_bpms
from ..common.rate_limit import limiter
from ..db import (
    TERMINAL_JOB_STATUSES,
    delete_jobs_and_share_links,
    find_active_job_for_submission,
    get_job,
    get_settings,
    get_stats,
    insert_job,
    list_completed_bpms,
    list_job_ids,
    paginate_jobs,
    set_settings,
    update_job,
    update_job_if_status,
    utc_timestamp,
)
from ..governor import governor
from ..session import delete_session_cookie, refresh_session_settings_cache
from ..utils.changelog import get_changelog_html
from ..utils.cookie_status import cookie_file_is_usable
from ..utils.cookies import default_cookie_file
from ..utils.credentials import normalize_admin_username, validate_admin_password
from ..utils.fs import get_data_dir, get_json_body, path_is_file
from ..utils.host_stats import get_host_stats
from ..utils.housekeeping import cleanup_job_directory
from ..utils.platform import PLATFORM_COOKIE_FILENAMES, detect_platform, validate_media_url
from ..utils.public_url import normalize_public_hostname
from ..utils.template_filters import is_lalala_configured, public_settings
from ..utils.updates import get_update_status
from ..utils.version import (
    get_ffmpeg_version,
    get_js_runtime_version,
    get_version,
    get_wavesurfer_version,
    get_ytdlp_ejs_version,
    get_ytdlp_version,
)
from ..utils.watermark_logo import (
    MAX_LOGO_BYTES,
    LogoRejectedError,
    LogoStatus,
    custom_logo_path,
    logo_status,
    remove_custom_logo,
    store_custom_logo,
)
from ..utils.youtube import (
    empty_info_payload,
    extract_video_meta_async,
    load_video_info_async,
    normalize_info_url,
)
from ..worker import cancel_job as cancel_worker_job
from ..worker import clear_cancellation, get_job_queue, submit_download
from .auth import (
    get_csrf_token,
    has_admin_credentials,
    hash_password,
    require_html_auth,
    require_user,
    require_user_json,
)
from .cookies import list_cookie_statuses

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


router = APIRouter(tags=["api"])

_ALLOWED_MEDIA_TYPES = frozenset({"audio", "video"})
_ALLOWED_QUALITIES = frozenset({"max", "medium", "small"})
_STATS_CACHE_TTL_SECONDS = 60.0
_PERSISTED_CANCELLABLE_JOB_STATUSES = (
    "queued",
    "processing",
    "downloading",
    "transcoding",
)
_RETRYABLE_JOB_STATUSES = frozenset({"error", "cancelled"})
_RUNTIME_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "download_worker_count": (0, 8),
    "download_timeout_minutes": (1, 240),
    "transcode_timeout_minutes": (1, 480),
    "download_max_filesize_gib": (1, 100),
    "audio_analysis_max_minutes": (0, 240),
    "audio_analysis_timeout_minutes": (1, 60),
    "lalal_max_download_gib": (1, 100),
    "session_idle_minutes": (1, 1440),
}

_templates: "Jinja2Templates | None" = None
_stats_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_stats_locks: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = WeakKeyDictionary()


def init_api(templates: "Jinja2Templates") -> None:
    global _templates
    _templates = templates



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


def _remove_job_artifacts(job_ids: list[str]) -> list[str]:
    """Remove only the job directories identified by the database snapshot."""
    data_dir = get_data_dir()
    return [job_id for job_id in job_ids if not cleanup_job_directory(job_id, data_dir)]


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


def job_to_dict(job: JobRecord) -> dict[str, Any]:
    """Convert a job record to a JSON-serializable dict."""
    return {
        "id": job["id"],
        "url": job["url"],
        "platform": detect_platform(job["url"]),
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


async def get_cached_stats() -> dict[str, int | float]:
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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/stats")
@limiter.limit("30/minute")
async def api_stats(request: Request, _: str = Depends(require_user_json)) -> dict[str, int | float]:
    """Return fresh dashboard stats for live UI updates.

    Annotated ``int | float``, not ``int``: total_minutes/total_lalal_minutes
    are rounded to one decimal, and FastAPI's response-model check 500s on a
    fractional value under ``dict[str, int]``.
    """
    _ = request
    return await asyncio.to_thread(get_stats)


@router.post("/api/stats/reset")
@limiter.limit("5/minute")
async def api_reset_stats(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    """Reset dashboard statistics without deleting jobs or their files."""
    _ = request

    async with _get_stats_lock():
        await asyncio.to_thread(
            set_settings,
            {"statistics_reset_at": utc_timestamp()},
            allow_internal=True,
        )
        _stats_cache["data"] = None
        _stats_cache["ts"] = 0.0

    return {"ok": True, "message": "Statistics reset"}


@router.post("/api/jobs/remove-all")
@limiter.limit("2/minute")
async def api_remove_all_jobs(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    """Cancel and permanently remove every current job, artifact, and share link."""
    _ = request

    job_ids = await asyncio.to_thread(list_job_ids)
    if not job_ids:
        return {
            "ok": True,
            "message": "No jobs to remove",
            "jobs_deleted": 0,
            "files_deleted": 0,
            "share_links_deleted": 0,
        }

    # A worker may still own a subprocess or be about to start one. Marking
    # every snapshot job cancelled makes its next cancellation check stop it;
    # subsequent database updates become harmless no-ops after row removal.
    for job_id in job_ids:
        cancel_worker_job(job_id)

    failed_cleanup = await asyncio.to_thread(_remove_job_artifacts, job_ids)
    if failed_cleanup:
        logger.error("Refusing to remove jobs after %d artifact cleanup failure(s)", len(failed_cleanup))
        raise HTTPException(
            status_code=500,
            detail="Could not remove every job file; no jobs or shared links were deleted",
        )

    async with _get_stats_lock():
        jobs_deleted, share_links_deleted = await asyncio.to_thread(
            delete_jobs_and_share_links,
            job_ids,
        )
        _stats_cache["data"] = None
        _stats_cache["ts"] = 0.0

    return {
        "ok": True,
        "message": f"Removed {jobs_deleted} job(s)",
        "jobs_deleted": jobs_deleted,
        "files_deleted": len(job_ids),
        "share_links_deleted": share_links_deleted,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    templates = _require_templates()

    raw_jobs = await asyncio.to_thread(paginate_jobs, limit=50, offset=0)
    jobs = [job_to_dict(job) for job in raw_jobs]
    stats = await get_cached_stats()
    settings = await asyncio.to_thread(get_settings, include_secrets=True)
    lalal_enabled = is_lalala_configured(settings)
    lalal_duration_guard = str(settings.get("lalalaai_duration_guard", "true")).lower() in ("true", "1", "yes")
    return templates.TemplateResponse(request=request, name="index.html", context={
        "jobs": jobs,
        "stats": stats,
        "lalal_enabled": lalal_enabled,
        "lalal_duration_guard": lalal_duration_guard,
        "auth_enabled": bool(settings.get("enable_authentication", False)),
        "csrf_token": get_csrf_token(request),
    })


def _cached_lalal_minutes(settings: dict[str, Any]) -> float | None:
    """Return the last known Lalal.ai balance, or None when it is unknown."""
    minutes = float(settings.get("lalalaai_minutes_left", -1) or -1)
    return minutes if minutes >= 0 else None


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    templates = _require_templates()

    settings = await asyncio.to_thread(get_settings, include_secrets=True)
    lalal_configured = is_lalala_configured(settings)
    ytdlp_version = await asyncio.to_thread(get_ytdlp_version)
    ytdlp_ejs_version = get_ytdlp_ejs_version()
    js_runtime_version = await asyncio.to_thread(get_js_runtime_version)
    ffmpeg_version = await asyncio.to_thread(get_ffmpeg_version)
    wavesurfer_version = get_wavesurfer_version()
    changelog_html = await asyncio.to_thread(get_changelog_html)
    # Reads internal settings itself; this page's snapshot excludes the hash.
    credentials_present = await asyncio.to_thread(has_admin_credentials)
    cookie_statuses = [status.model_dump() for status in await list_cookie_statuses()]
    # What "Automatic" resolves to right now, so the hint names a real number.
    auto_fragments = await asyncio.to_thread(governor.recommended_concurrent_fragments)
    # Mobile shows the dashboard stat tiles on the System tab (see macros/stats.html).
    stats = await get_cached_stats()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": public_settings(settings),
            "stats": stats,
            "auto_concurrent_fragments": auto_fragments,
            "lalal_configured": lalal_configured,
            "lalal_status": "Connected" if lalal_configured else "Not configured",
            "lalal_email": str(settings.get("lalalaai_email", "") or ""),
            # -1 is the stored "not known yet"; the page renders no balance for
            # it and fills one in once /api/lalal/status answers.
            "lalal_minutes_left": _cached_lalal_minutes(settings),
            "ytdlp_version": ytdlp_version,
            "ytdlp_ejs_version": ytdlp_ejs_version,
            "js_runtime_version": js_runtime_version,
            "ffmpeg_version": ffmpeg_version,
            "wavesurfer_version": wavesurfer_version,
            "changelog_html": changelog_html,
            "admin_username": str(settings.get("admin_username", "") or ""),
            "has_admin_credentials": credentials_present,
            "auth_enabled": bool(settings.get("enable_authentication", False)),
            "cookie_statuses": cookie_statuses,
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/api/updates")
@limiter.limit("30/minute")
async def api_updates(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    """Report whether newer upstream releases exist for fetchly and its tools.

    Informational only - everything is baked into the image; only a rebuild
    changes it. The upstream lookup is cached for 24h.
    """
    _ = request

    current = {
        "fetchly": get_version(),
        "ytdlp": await asyncio.to_thread(get_ytdlp_version),
        "ytdlp_ejs": get_ytdlp_ejs_version(),
        "js_runtime": await asyncio.to_thread(get_js_runtime_version),
        "ffmpeg": await asyncio.to_thread(get_ffmpeg_version),
        "wavesurfer": get_wavesurfer_version(),
    }
    return await get_update_status(current)


@router.get("/api/system/host")
@limiter.limit("60/minute")
async def api_system_host(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    """Host resource snapshot for Settings -> System (disk, CPU, memory, uptime).

    Each field is null when the host does not expose it. CPU is the delta since
    the previous call, so polling every few seconds gives live load.
    """
    _ = request
    return await get_host_stats(get_data_dir())


@router.get("/api/jobs")
@limiter.limit("60/minute")
async def api_jobs(
    request: Request,
    _user: str = Depends(require_user),
    offset: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    _ = request

    safe_offset = max(0, offset)
    safe_limit = min(max(1, limit), 100)

    jobs = await asyncio.to_thread(paginate_jobs, limit=safe_limit, offset=safe_offset)
    return [job_to_dict(job) for job in jobs]


@router.get("/api/jobs/{job_id}")
@limiter.limit("60/minute")
async def api_job(request: Request, job_id: uuid.UUID, _user: str = Depends(require_user)) -> dict[str, Any]:
    _ = request

    job = await asyncio.to_thread(get_job, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job_to_dict(job)


@router.get("/api/stats/bpm-clusters")
@limiter.limit("30/minute")
async def api_bpm_clusters(
    request: Request,
    _user: str = Depends(require_user),
    limit: int = 1000,
) -> list[dict[str, int]]:
    _ = request
    safe_limit = min(max(1, limit), 2000)
    bpms = await asyncio.to_thread(list_completed_bpms, safe_limit)
    clusters = await asyncio.to_thread(cluster_bpms, bpms)
    return [
        {"bpm": bpm_bucket, "count": count}
        for bpm_bucket, count in clusters
    ]


async def _tiktok_oembed_thumbnail(url: str) -> str | None:
    """Fetch thumbnail from TikTok oEmbed API (no cookies/yt-dlp needed)."""
    oembed_url = f"https://www.tiktok.com/oembed?url={quote(url, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, max_redirects=3) as client:
            resp = await client.get(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        thumb = str(data.get("thumbnail_url") or "").strip()
        return thumb or None
    except Exception as exc:
        logger.debug("TikTok oEmbed failed for %s: %s", url, exc)
        return None


@router.get("/api/info")
@limiter.limit("20/minute")
async def api_info(request: Request, url: str, _user: str = Depends(require_user)) -> dict[str, Any]:
    """Extract video metadata using yt-dlp."""
    _ = request
    is_valid, error_msg = validate_media_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    try:
        info_url = normalize_info_url(url)
        info = await asyncio.wait_for(load_video_info_async(info_url), timeout=20.0)

        if not info:
            # TikTok: try oEmbed even when yt-dlp returns nothing
            platform = detect_platform(url)
            if platform == "tiktok":
                thumb = await _tiktok_oembed_thumbnail(url)
                if thumb:
                    payload = empty_info_payload()
                    payload["thumbnail"] = thumb
                    payload["platform"] = platform
                    payload["unavailable"] = False
                    return payload
            return empty_info_payload()

        title = str(info.get("title") or "").strip()
        channel = str(info.get("channel") or "").strip()
        uploader = str(info.get("uploader") or "").strip()
        duration = info.get("duration")
        views = info.get("view_count")
        thumbnail = str(info.get("thumbnail") or "").strip()

        # TikTok: yt-dlp often returns no thumbnail — try oEmbed as fallback
        platform = detect_platform(url)
        if not thumbnail and platform == "tiktok":
            thumbnail = await _tiktok_oembed_thumbnail(url) or ""

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
            "thumbnail": thumbnail or None,
            "platform": platform,
            "formats": formats[:20],
            "formats_total": len(formats),
            "formats_truncated": len(formats) > 20,
            "unavailable": False,
        }

    except TimeoutError:
        logger.warning("Video info extraction timed out for URL %s", url)
        return empty_info_payload()
    except OSError as exc:
        logger.exception("System error during video info extraction for URL %s", url)
        raise HTTPException(status_code=503, detail="Metadata extraction temporarily unavailable") from exc
    except Exception as exc:
        logger.warning("Video info extraction failed for URL %s: %s", url, exc, exc_info=True)
        return empty_info_payload()


_THUMBNAIL_ALLOWED_SUFFIXES: frozenset[str] = frozenset({
    # TikTok CDN domains
    ".tiktokcdn.com",
    ".tiktokcdn-us.com",
    ".ttwstatic.com",
    ".tiktokv.com",
    ".byteimg.com",
    ".ibyteimg.com",
    ".muscdn.com",
    # Instagram / Facebook CDN domains
    ".cdninstagram.com",
    ".fbcdn.net",
    ".facebook.com",
})
_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
_THUMBNAIL_ALLOWED_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
})
_THUMB_CACHE_DIR = get_data_dir() / "thumb-cache"
_THUMB_CACHE_KEY_RE = re.compile(r"^[a-f0-9]{64}$")
_THUMB_EXTRACTION_TIMEOUT_SECONDS = 30
_THUMB_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
def _thumbnail_signature_matches(data: bytes, content_type: str) -> bool:
    """Reject payloads whose bytes do not match their declared raster type."""
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _is_allowed_thumbnail_url(url: str) -> bool:
    """Allowlist check for outbound thumbnail fetches.

    Parsed with httpx.URL - the same parser the request uses - so the validated
    host is exactly the contacted host (urlparse disagrees on enough edge cases
    to open a gap).
    """
    try:
        parsed = httpx.URL(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.host or "").lower()
        return any(host == s.lstrip(".") or host.endswith(s) for s in _THUMBNAIL_ALLOWED_SUFFIXES)
    except Exception:
        return False


def _thumb_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cookies_args_for_url(url: str) -> list[str]:
    """Cookie args for a thumbnail fetch, gated like a download: a usable jar
    only (an expired one is still sent by yt-dlp and answered worse than
    anonymous). Same rule in app/worker.py and app/utils/youtube.py.
    """
    platform = detect_platform(url)
    if not platform:
        return []
    cookie_path = _resolve_cookie_file(platform)
    if cookie_path and cookie_file_is_usable(cookie_path, platform):
        return ["--cookies", str(cookie_path)]
    return []


def _resolve_cookie_file(platform: str) -> Path | None:
    filename = PLATFORM_COOKIE_FILENAMES.get(platform)
    if not filename:
        return None

    return default_cookie_file(filename)


def _cookie_hint_for_url(url: str) -> str | None:
    platform = detect_platform(url)
    if platform not in PLATFORM_COOKIE_FILENAMES:
        return None
    cookie_path = _resolve_cookie_file(platform)
    if cookie_path and not cookie_path.is_file():
        return f"{platform}_cookies_missing"
    return None


def _thumb_cache_paths(cache_key: str) -> tuple[Path, Path]:
    return (
        _THUMB_CACHE_DIR / f"{cache_key}.bin",
        _THUMB_CACHE_DIR / f"{cache_key}.ct",
    )


def _read_cached_thumbnail(cache_key: str) -> tuple[bytes, str] | None:
    data_path, content_type_path = _thumb_cache_paths(cache_key)
    if not data_path.is_file() or not content_type_path.is_file():
        return None

    try:
        data = data_path.read_bytes()
        content_type = content_type_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not data or len(data) > _THUMBNAIL_MAX_BYTES:
        return None
    if content_type not in _THUMBNAIL_ALLOWED_TYPES:
        return None
    if not _thumbnail_signature_matches(data, content_type):
        return None
    return data, content_type


def _write_cached_thumbnail(cache_key: str, data: bytes, content_type: str) -> None:
    """Publish a thumbnail via unique temp files + atomic rename.

    The temp names carry a random token: the cache key is shared by concurrent
    resolves of the same URL, so a key-derived temp name would let them
    overwrite each other mid-write.
    """
    _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_path, content_type_path = _thumb_cache_paths(cache_key)
    token = uuid.uuid4().hex
    tmp_data = data_path.parent / f"{data_path.name}.{token}.tmp"
    tmp_content_type = content_type_path.parent / f"{content_type_path.name}.{token}.tmp"
    try:
        tmp_data.write_bytes(data)
        tmp_content_type.write_text(content_type, encoding="utf-8")
        tmp_data.replace(data_path)
        tmp_content_type.replace(content_type_path)
    finally:
        tmp_data.unlink(missing_ok=True)
        tmp_content_type.unlink(missing_ok=True)


async def _fetch_thumbnail_payload(url: str) -> tuple[bytes, str]:
    if not _is_allowed_thumbnail_url(url):
        raise HTTPException(status_code=400, detail="Thumbnail URL not allowed")

    try:
        async with (
            httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client,
            client.stream("GET", url, headers={"User-Agent": "fetchly"}) as resp,
        ):
                if 300 <= resp.status_code < 400:
                    raise HTTPException(status_code=502, detail="Thumbnail redirects are not accepted")
                if resp.status_code != 200:
                    raise HTTPException(status_code=502, detail="Thumbnail fetch failed")

                content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in _THUMBNAIL_ALLOWED_TYPES:
                    raise HTTPException(status_code=502, detail="Unsupported thumbnail type")

                content_length = resp.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                        if declared_length < 0:
                            raise HTTPException(status_code=502, detail="Invalid thumbnail size")
                        if declared_length > _THUMBNAIL_MAX_BYTES:
                            raise HTTPException(status_code=502, detail="Thumbnail too large")
                    except ValueError as exc:
                        raise HTTPException(status_code=502, detail="Invalid thumbnail size") from exc

                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    if len(data) + len(chunk) > _THUMBNAIL_MAX_BYTES:
                        raise HTTPException(status_code=502, detail="Thumbnail too large")
                    data.extend(chunk)
    except httpx.RequestError as exc:
        logger.warning("Thumbnail fetch failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Thumbnail unavailable") from exc
    if not data:
        raise HTTPException(status_code=502, detail="Thumbnail unavailable")
    if not _thumbnail_signature_matches(data, content_type):
        raise HTTPException(status_code=502, detail="Invalid thumbnail payload")
    return bytes(data), content_type


def _content_type_from_suffix(suffix: str) -> str:
    ext = suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "image/jpeg"


def _extract_thumbnail_with_ytdlp(url: str) -> tuple[bytes, str] | None:
    with tempfile.TemporaryDirectory(prefix="thumb-resolve-") as tmp_dir:
        out_template = str(Path(tmp_dir) / "thumb.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            *_cookies_args_for_url(url),
            "-o",
            out_template,
            "--",
            url,
        ]

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=False,
                timeout=_THUMB_EXTRACTION_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None

        tmp_path = Path(tmp_dir)
        candidates = sorted(
            p for p in tmp_path.glob("thumb.*")
            if p.is_file() and p.suffix.lower() in _THUMB_ALLOWED_EXTENSIONS
        )
        if not candidates:
            return None

        thumb_path = candidates[0]
        try:
            if thumb_path.stat().st_size > _THUMBNAIL_MAX_BYTES:
                return None
            data = thumb_path.read_bytes()
        except OSError:
            return None

        if not data:
            return None
        return data, _content_type_from_suffix(thumb_path.suffix)


@router.get("/api/thumbnail/resolve")
@limiter.limit("30/minute")
async def api_thumbnail_resolve(
    request: Request,
    url: str,
    _user: str = Depends(require_user),
) -> dict[str, object]:
    """Resolve a media URL to a persistent local thumbnail-cache URL."""
    _ = request

    is_valid, error_msg = validate_media_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    info_url = normalize_info_url(url)
    cache_key = _thumb_cache_key(info_url)
    cached = await asyncio.to_thread(_read_cached_thumbnail, cache_key)
    if cached is not None:
        return {
            "thumbnail_url": f"/api/thumbnail-cache/{cache_key}",
            "cached": True,
            "unavailable": False,
            "reason": None,
        }

    platform = detect_platform(info_url)
    thumbnail_url = ""
    if platform == "tiktok":
        thumbnail_url = await _tiktok_oembed_thumbnail(info_url) or ""

    if not thumbnail_url:
        # Best-effort only: a timeout or extractor error here must fall through
        # to the yt-dlp thumbnail extraction below, not surface as a 500.
        try:
            info = await asyncio.wait_for(load_video_info_async(info_url), timeout=20.0)
            if info:
                thumbnail_url = str(info.get("thumbnail") or "").strip()
        except Exception as exc:
            logger.debug("Metadata lookup failed while resolving thumbnail for %s: %s", info_url, exc)

    data: bytes | None = None
    content_type = "image/jpeg"
    if thumbnail_url:
        try:
            data, content_type = await _fetch_thumbnail_payload(thumbnail_url)
        except HTTPException:
            data = None

    if data is None:
        extracted = await asyncio.to_thread(_extract_thumbnail_with_ytdlp, info_url)
        if extracted is not None:
            data, content_type = extracted

    if data is None:
        return {
            "thumbnail_url": None,
            "cached": False,
            "unavailable": True,
            "reason": _cookie_hint_for_url(info_url),
        }

    await asyncio.to_thread(_write_cached_thumbnail, cache_key, data, content_type)
    return {
        "thumbnail_url": f"/api/thumbnail-cache/{cache_key}",
        "cached": False,
        "unavailable": False,
        "reason": None,
    }


@router.get("/api/thumbnail-cache/{cache_key}")
@limiter.limit("120/minute")
async def api_thumbnail_cache(
    request: Request,
    cache_key: str,
    _user: str = Depends(require_user),
) -> Response:
    _ = request
    if not _THUMB_CACHE_KEY_RE.fullmatch(cache_key):
        raise HTTPException(status_code=400, detail="Invalid cache key")

    cached = await asyncio.to_thread(_read_cached_thumbnail, cache_key)
    if cached is None:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    data, content_type = cached
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/thumbnail-proxy")
@limiter.limit("30/minute")
async def api_thumbnail_proxy(
    request: Request,
    url: str,
    _user: str = Depends(require_user),
) -> Response:
    """Proxy an external thumbnail URL server-side (CSP-safe for TikTok/Instagram)."""
    _ = request
    data, content_type = await _fetch_thumbnail_payload(url)
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/settings")
@limiter.limit("60/minute")
async def api_get_settings(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    _ = request
    settings = await asyncio.to_thread(get_settings, include_secrets=True)
    return public_settings(settings)


def _logo_status_payload(status: LogoStatus) -> dict[str, Any]:
    return {
        "custom": status.custom,
        "width": status.width,
        "height": status.height,
        "bytes": status.bytes,
        "fingerprint": status.fingerprint,
    }


@router.get("/api/settings/watermark-logo")
@limiter.limit("60/minute")
async def api_watermark_logo_status(
    request: Request, _user: str = Depends(require_user)
) -> dict[str, Any]:
    """Whether a custom watermark logo is installed, and its size."""
    _ = request
    return _logo_status_payload(await asyncio.to_thread(logo_status))


@router.get("/api/settings/watermark-logo/image")
@limiter.limit("60/minute")
async def api_watermark_logo_image(request: Request, _user: str = Depends(require_user)) -> Response:
    """The stored custom logo, for the Settings preview.

    Only ever a PNG - the upload path stores nothing else - and served with
    nosniff so it cannot be coaxed into being interpreted as a document.
    """
    _ = request
    path = custom_logo_path()
    if not await path_is_file(path):
        raise HTTPException(status_code=404, detail="No custom watermark logo")

    data = await asyncio.to_thread(path.read_bytes)
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "X-Content-Type-Options": "nosniff",
            # The fingerprint is in the URL the page requests, so the bytes
            # behind it never change; revalidation would be wasted.
            "Cache-Control": "private, max-age=60",
        },
    )


@router.post("/api/settings/watermark-logo")
@limiter.limit("10/minute")
async def api_watermark_logo_upload(
    request: Request,
    file: UploadFile = File(...),
    _user: str = Depends(require_user),
) -> dict[str, Any]:
    """Install a custom watermark logo.

    Always a PNG: the Settings page uploads a dropped PNG as it is and
    rasterizes a dropped SVG in the browser first. Every property it has to
    have is re-derived from these bytes rather than taken from the client (see
    utils/watermark_logo.py).
    """
    _ = request
    body = await file.read(MAX_LOGO_BYTES + 1)
    if len(body) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="That image is too large")

    try:
        status = await asyncio.to_thread(store_custom_logo, body)
    except LogoRejectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("Could not store the custom watermark logo: %s", exc)
        raise HTTPException(status_code=500, detail="Could not store the logo") from exc

    return _logo_status_payload(status)


@router.delete("/api/settings/watermark-logo")
@limiter.limit("10/minute")
async def api_watermark_logo_delete(
    request: Request, _user: str = Depends(require_user)
) -> dict[str, Any]:
    """Drop the custom logo; the bundled fetchly artwork applies again."""
    _ = request
    try:
        removed = await asyncio.to_thread(remove_custom_logo)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not remove the logo") from exc
    return {"removed": removed, **_logo_status_payload(LogoStatus(custom=False))}


def _parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise HTTPException(status_code=400, detail=f"{name} must be a boolean")


# response_model=None: the annotated union includes a JSONResponse (password
# changes clear the session cookie on the way out), which FastAPI cannot turn
# into a schema. The annotation is there for the type checker, not for OpenAPI.
@router.post("/api/settings", response_model=None)
@limiter.limit("5/minute")
async def api_set_settings(
    request: Request,
    _user: str = Depends(require_user),
) -> dict[str, Any] | JSONResponse:
    """Update retention/download/session settings and optional admin password."""
    payload = await get_json_body(request)

    settings_to_update: dict[str, Any] = {}
    credentials_changed = False

    if "retention_days" in payload:
        settings_to_update["retention_days"] = _clamp_int(payload["retention_days"], 0, 365, "retention_days")

    if "download_concurrent_fragments" in payload:
        settings_to_update["download_concurrent_fragments"] = _clamp_int(
            payload["download_concurrent_fragments"], 0, 16, "download_concurrent_fragments"
        )

    if "share_link_max_uses" in payload:
        settings_to_update["share_link_max_uses"] = _clamp_int(
            payload["share_link_max_uses"], 0, 10000, "share_link_max_uses"
        )

    for key, (min_value, max_value) in _RUNTIME_LIMIT_BOUNDS.items():
        if key in payload:
            settings_to_update[key] = _clamp_int(payload[key], min_value, max_value, key)

    if "public_hostname" in payload:
        try:
            settings_to_update["public_hostname"] = normalize_public_hostname(
                str(payload["public_hostname"] or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Public hostname: {exc}") from exc

    if "download_compatible_output" in payload:
        enabled = _parse_bool(payload["download_compatible_output"], "download_compatible_output")
        settings_to_update["download_compatible_output"] = "true" if enabled else "false"

    if "video_watermark" in payload:
        enabled = _parse_bool(payload["video_watermark"], "video_watermark")
        settings_to_update["video_watermark"] = "true" if enabled else "false"

    if "lalalaai_duration_guard" in payload:
        enabled = _parse_bool(payload["lalalaai_duration_guard"], "lalalaai_duration_guard")
        settings_to_update["lalalaai_duration_guard"] = "true" if enabled else "false"

    want_authentication: bool | None = None
    if "enable_authentication" in payload:
        want_authentication = _parse_bool(payload["enable_authentication"], "enable_authentication")

    try:
        current_settings = await asyncio.to_thread(get_settings, include_internal=True)
        auth_was_enabled = bool(current_settings.get("enable_authentication", False))

        # Credentials are always set as a pair: the PBKDF2 salt is derived from
        # the username, so a rename has to re-hash, which needs the plaintext.
        if payload.get("admin_password") or payload.get("admin_username"):
            try:
                new_username = normalize_admin_username(str(payload.get("admin_username") or ""))
                new_password = validate_admin_password(str(payload.get("admin_password") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not new_username:
                raise HTTPException(status_code=400, detail="Username is required")

            settings_to_update["admin_username"] = new_username
            settings_to_update["admin_password_hash"] = await asyncio.to_thread(
                hash_password,
                new_username,
                new_password,
            )
            # Invalidate every existing session: the credential they were
            # issued against no longer exists.
            settings_to_update["session_version"] = (
                int(current_settings.get("session_version", 0) or 0) + 1
            )
            credentials_changed = True

        if want_authentication is not None:
            # Turning authentication on without an account would brick the
            # instance: nobody could log in and every page would redirect to a
            # login that rejects everything.
            if want_authentication and not credentials_changed and not has_admin_credentials(current_settings):
                raise HTTPException(
                    status_code=400,
                    detail="Set a username and password before enabling authentication",
                )
            settings_to_update["enable_authentication"] = "true" if want_authentication else "false"

        if settings_to_update:
            await asyncio.to_thread(set_settings, settings_to_update, allow_internal=True)
            await asyncio.to_thread(refresh_session_settings_cache)

        auth_is_enabled = want_authentication if want_authentication is not None else auth_was_enabled
        # Only bounce to the login page when there is actually a login to
        # perform. Changing credentials while authentication is off would
        # otherwise redirect to /login, which immediately sends the user back.
        if auth_is_enabled and (credentials_changed or not auth_was_enabled):
            message = (
                "Authentication enabled. Please sign in."
                if not auth_was_enabled
                else "Credentials updated. Please log in again."
            )
            response = JSONResponse(
                content={"ok": True, "message": message, "redirect": "/login"}
            )
            delete_session_cookie(response, request)
            return response

        if credentials_changed:
            return {"ok": True, "message": "Credentials updated"}
        return {"ok": True, "message": "Settings updated"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Settings update failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


# response_model=None: see api_set_settings - the 409 duplicate-job branch
# returns a JSONResponse, so the union cannot be schema-generated.
@router.post("/api/submit", response_model=None)
@limiter.limit("10/minute")
async def api_submit(
    request: Request,
    url: str = Form(...),
    media_type: str = Form(..., alias="type"),
    quality: str = Form(...),
    confirm_duplicate: bool = Form(False),
    _user: str = Depends(require_user),
) -> dict[str, Any] | JSONResponse:
    _ = request
    media_type = str(media_type).strip().lower()
    quality_value = str(quality).strip().lower()

    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Allowed: {', '.join(sorted(_ALLOWED_MEDIA_TYPES))}")
    if quality_value not in _ALLOWED_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid quality. Allowed: {', '.join(sorted(_ALLOWED_QUALITIES))}",
        )

    is_valid, error_msg = validate_media_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    clean_url = url.strip()

    # Same (url, type, quality) already downloaded or in flight: return a 409
    # rather than silently making a second job. Errored/cancelled don't count
    # (find_active_job_for_submission), so a retry is never blocked; the
    # frontend re-submits with confirm_duplicate=true on confirmation.
    if not confirm_duplicate:
        existing_job = await asyncio.to_thread(
            find_active_job_for_submission, clean_url, media_type, quality_value
        )
        if existing_job is not None:
            return JSONResponse(
                status_code=409,
                content={"detail": "duplicate_job", "existing_job": job_to_dict(existing_job)},
            )

    # Best-effort metadata; job creation must not block on it.
    meta: dict[str, object] = {"video_title": None, "video_meta_hover": None, "duration_seconds": None}
    try:
        meta = await asyncio.wait_for(extract_video_meta_async(clean_url), timeout=8.0)
    except Exception as exc:
        logger.debug("Metadata extraction skipped for submit (will be fetched by worker): %s", exc)

    job_id = str(uuid.uuid4())
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
        # Source runtime, so the job shows a length while queued; the worker
        # replaces it with the ffprobe value.
        duration_seconds=meta.get("duration_seconds"),
    )
    if not submit_download((job_id, clean_url, media_type, quality_value)):
        try:
            await asyncio.to_thread(
                update_job,
                job_id,
                status="error",
                message="Queue full at submission time",
                finished_at=utc_timestamp(),
            )
        except Exception:
            logger.exception("Failed to mark job %s as errored after queue overflow", job_id)
        raise HTTPException(status_code=503, detail="Job queue is full, please try again later")
    job = await asyncio.to_thread(get_job, job_id)
    if not job:
        # Row vanished between insert and read (retention sweep or manual
        # delete); the job is queued, but there is nothing to hand back.
        raise HTTPException(status_code=500, detail="Submitted job could not be loaded")
    return job_to_dict(job)


@router.post("/api/jobs/{job_id}/cancel")
@limiter.limit("30/minute")
async def cancel_job(
    request: Request,
    job_id: uuid.UUID,
    _user: str = Depends(require_user_json),
) -> dict[str, Any]:
    _ = request

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job["status"] or ""
    if status in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot cancel job with status: {status}")

    cancelled = await asyncio.to_thread(
        update_job_if_status,
        job_id_str,
        _PERSISTED_CANCELLABLE_JOB_STATUSES,
        status="cancelled",
        message="Cancelled by user",
        finished_at=utc_timestamp(),
    )

    if not cancelled:
        raise HTTPException(status_code=409, detail="Job state changed before cancellation")

    # Terminates the job's subprocess with a SIGTERM/SIGKILL grace period, so it
    # blocks for up to ~2s - off the event loop, like every other call here.
    await asyncio.to_thread(cancel_worker_job, job_id_str)
    logger.info("Cancellation requested for job %s (status: %s)", job_id_str, status)

    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_dict(job)


@router.post("/api/jobs/{job_id}/retry")
@limiter.limit("10/minute")
async def retry_job(
    request: Request,
    job_id: uuid.UUID,
    _user: str = Depends(require_user_json),
) -> dict[str, Any]:
    """Re-queue a failed/cancelled job in place, keeping its row and id.

    Only from "error"/"cancelled" (retrying an in-flight or done job would
    race the worker or duplicate a download). The row is reset - not replaced -
    so it refreshes in place, and every result field from the failed run is
    cleared so no stale data shows.
    """
    _ = request

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job["status"] or ""
    if status not in _RETRYABLE_JOB_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot retry job with status: {status}")

    job_queue = get_job_queue()
    if job_queue.full():
        raise HTTPException(status_code=503, detail="Job queue is full, please try again later")

    requeued = await asyncio.to_thread(
        update_job_if_status,
        job_id_str,
        (status,),
        status="queued",
        message="",
        filename=None,
        finished_at=None,
        filesize_bytes=None,
        duration_seconds=None,
        codec=None,
        bitrate_kbps=None,
        bpm=None,
        bpm_confidence=None,
        audio_hash=None,
        lalal_split_done=0,
    )
    if not requeued:
        raise HTTPException(status_code=409, detail="Job state changed before retry")

    # A job cancelled while queued still carries its in-memory cancel marker
    # (and maybe its queue entry); both would cancel the retry again.
    clear_cancellation(job_id_str)

    if not submit_download((job_id_str, str(job["url"]), str(job["type"]), str(job["quality"]))):
        try:
            await asyncio.to_thread(
                update_job,
                job_id_str,
                status="error",
                message="Queue full at retry time",
                finished_at=utc_timestamp(),
            )
        except Exception:
            logger.exception("Failed to mark job %s as errored after retry queue overflow", job_id_str)
        raise HTTPException(status_code=503, detail="Job queue is full, please try again later")

    logger.info("Retry requested for job %s (was: %s)", job_id_str, status)

    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_dict(job)
