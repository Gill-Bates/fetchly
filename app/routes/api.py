#!/usr/bin/env python3
#
# app/routes/api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Core API routes for jobs, settings, and info."""

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import tempfile
from queue import Full
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Protocol
from weakref import WeakKeyDictionary

import httpx

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..common.rate_limit import limiter
from ..bpm_cluster import cluster_bpms
from ..db import (
    TERMINAL_JOB_STATUSES,
    find_active_job_for_submission,
    get_job,
    get_settings,
    get_stats,
    insert_job,
    list_completed_bpms,
    paginate_jobs,
    set_settings,
    update_job,
    update_job_if_status,
    utc_timestamp,
)
from ..utils.fs import get_data_dir, get_json_body
from ..session import delete_session_cookie, refresh_session_settings_cache
from ..utils.template_filters import is_lalala_configured, public_settings
from ..utils.platform import detect_platform, validate_media_url
from ..utils.updates import get_update_status
from ..utils.version import (
    get_ffmpeg_version,
    get_js_runtime_version,
    get_wavesurfer_version,
    get_ytdlp_ejs_version,
    get_ytdlp_version,
)
from ..utils.youtube import (
    empty_info_payload,
    extract_video_meta_async,
    load_video_info_async,
    normalize_info_url,
)
from ..worker import cancel_job as cancel_worker_job, get_job_queue
from .auth import (
    hash_password,
    require_html_auth,
    require_session,
    require_user,
    require_user_json,
)

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


router = APIRouter(tags=["api"])

# Constants
_ALLOWED_MEDIA_TYPES = frozenset({"audio", "video"})
_ALLOWED_QUALITIES = frozenset({"max", "medium", "small"})
_STATS_CACHE_TTL_SECONDS = 60.0
_PERSISTED_CANCELLABLE_JOB_STATUSES = (
    "queued",
    "processing",
    "downloading",
    "transcoding",
)

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


@router.post("/api/stats/reset")
@limiter.limit("5/minute")
async def api_reset_stats(request: Request, _user: str = Depends(require_session)):
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


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Dashboard home page."""
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
        "csrf_token": getattr(request.state, "csrf_token", ""),
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
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
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": public_settings(settings),
            "lalal_configured": lalal_configured,
            "lalal_status": "Connected" if lalal_configured else "Not configured",
            "lalal_email": str(settings.get("lalalaai_email", "") or ""),
            "ytdlp_version": ytdlp_version,
            "ytdlp_ejs_version": ytdlp_ejs_version,
            "js_runtime_version": js_runtime_version,
            "ffmpeg_version": ffmpeg_version,
            "wavesurfer_version": wavesurfer_version,
            "csrf_token": getattr(request.state, "csrf_token", ""),
        },
    )


@router.get("/api/updates")
@limiter.limit("30/minute")
async def api_updates(request: Request, _user: str = Depends(require_user)):
    """Report whether newer upstream releases exist for the installed tools.

    Purely informational: nothing here updates anything, since these tools are
    baked into the image and only a rebuild can change them. The upstream
    lookup is cached for 24 hours, so reloading the settings page does not
    trigger another request.
    """
    _ = request

    current = {
        "ytdlp": await asyncio.to_thread(get_ytdlp_version),
        "ytdlp_ejs": get_ytdlp_ejs_version(),
        "js_runtime": await asyncio.to_thread(get_js_runtime_version),
        "ffmpeg": await asyncio.to_thread(get_ffmpeg_version),
        "wavesurfer": get_wavesurfer_version(),
    }
    return await get_update_status(current)


@router.get("/api/jobs")
@limiter.limit("60/minute")
async def api_jobs(request: Request, _user: str = Depends(require_user), offset: int = 0, limit: int = 50):
    """List jobs with pagination."""
    _ = request

    safe_offset = max(0, offset)
    safe_limit = min(max(1, limit), 100)

    jobs = await asyncio.to_thread(paginate_jobs, limit=safe_limit, offset=safe_offset)
    return [job_to_dict(job) for job in jobs]


@router.get("/api/jobs/{job_id}")
@limiter.limit("60/minute")
async def api_job(request: Request, job_id: uuid.UUID, _user: str = Depends(require_user)):
    """Return a single job snapshot by id."""
    _ = request

    job = await asyncio.to_thread(get_job, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job_to_dict(job)


@router.get("/api/stats/bpm-clusters")
@limiter.limit("30/minute")
async def api_bpm_clusters(request: Request, _user: str = Depends(require_user), limit: int = 1000):
    """Get BPM clusters for visualization."""
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
    from urllib.parse import quote

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
async def api_info(request: Request, url: str, _user: str = Depends(require_user)):
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

    except asyncio.TimeoutError:
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
_COOKIES_DIR = Path(__file__).parent.parent.parent
_COOKIES_DATA_DIR = get_data_dir()
_PLATFORM_COOKIE_FILENAMES: dict[str, str] = {
    "youtube": "youtube_cookies.txt",
    "instagram": "instagram_cookies.txt",
    "tiktok": "tiktok_cookies.txt",
}


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
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        return any(host == s.lstrip(".") or host.endswith(s) for s in _THUMBNAIL_ALLOWED_SUFFIXES)
    except Exception:
        return False


def _thumb_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cookies_args_for_url(url: str) -> list[str]:
    platform = detect_platform(url)
    if not platform:
        return []
    cookie_path = _resolve_cookie_file(platform)
    if cookie_path and cookie_path.is_file():
        return ["--cookies", str(cookie_path)]
    return []


def _resolve_cookie_file(platform: str) -> Path | None:
    filename = _PLATFORM_COOKIE_FILENAMES.get(platform)
    if not filename:
        return None

    custom_dir_raw = os.environ.get("TUBEYOU_COOKIES_DIR", "").strip()
    custom_dir = Path(custom_dir_raw) if custom_dir_raw else None
    for directory in (custom_dir, _COOKIES_DIR, _COOKIES_DATA_DIR):
        if directory is None:
            continue
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return _COOKIES_DIR / filename


def _cookie_hint_for_url(url: str) -> str | None:
    platform = detect_platform(url)
    if platform not in {"instagram", "tiktok", "youtube"}:
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
    _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_path, content_type_path = _thumb_cache_paths(cache_key)
    tmp_data = data_path.with_suffix(data_path.suffix + ".tmp")
    tmp_content_type = content_type_path.with_suffix(content_type_path.suffix + ".tmp")
    tmp_data.write_bytes(data)
    tmp_content_type.write_text(content_type, encoding="utf-8")
    tmp_data.replace(data_path)
    tmp_content_type.replace(content_type_path)


async def _fetch_thumbnail_payload(url: str) -> tuple[bytes, str]:
    if not _is_allowed_thumbnail_url(url):
        raise HTTPException(status_code=400, detail="Thumbnail URL not allowed")

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": "tubeyou"}) as resp:
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
                    except ValueError:
                        raise HTTPException(status_code=502, detail="Invalid thumbnail size")

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
    """Resolve a media URL to a local persistent thumbnail cache URL."""
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
        info = await asyncio.wait_for(load_video_info_async(info_url), timeout=20.0)
        if info:
            thumbnail_url = str(info.get("thumbnail") or "").strip()

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
    """Serve a previously cached thumbnail by cache key."""
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
async def api_get_settings(request: Request, _user: str = Depends(require_user)):
    """Get all settings."""
    _ = request
    settings = await asyncio.to_thread(get_settings, include_secrets=True)
    return public_settings(settings)


def _parse_bool(value: Any, name: str) -> bool:
    """Parse a value as boolean, handling string values robustly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise HTTPException(status_code=400, detail=f"{name} must be a boolean")


@router.post("/api/settings")
@limiter.limit("5/minute")
async def api_set_settings(request: Request, _user: str = Depends(require_session)):
    """Update retention/download/session settings and optional admin password."""
    payload = await get_json_body(request)

    settings_to_update: dict[str, Any] = {}
    password_changed = False

    if "retention_days" in payload:
        settings_to_update["retention_days"] = _clamp_int(payload["retention_days"], 1, 365, "retention_days")

    if "download_concurrent_fragments" in payload:
        settings_to_update["download_concurrent_fragments"] = _clamp_int(
            payload["download_concurrent_fragments"], 1, 16, "download_concurrent_fragments"
        )

    if "download_mp4_preset" in payload:
        enabled = _parse_bool(payload["download_mp4_preset"], "download_mp4_preset")
        settings_to_update["download_mp4_preset"] = "true" if enabled else "false"

    if "lalalaai_duration_guard" in payload:
        enabled = _parse_bool(payload["lalalaai_duration_guard"], "lalalaai_duration_guard")
        settings_to_update["lalalaai_duration_guard"] = "true" if enabled else "false"

    try:
        if "admin_password" in payload and payload["admin_password"]:
            new_password = str(payload["admin_password"]).strip()
            if len(new_password) < 8:
                raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

            current_settings = await asyncio.to_thread(get_settings, include_internal=True)
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
    confirm_duplicate: bool = Form(False),
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

    # Validate media URL (platform detected from URL: YouTube / TikTok / Instagram)
    is_valid, error_msg = validate_media_url(url)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    clean_url = url.strip()

    # Same (url, type, quality) already downloaded or in flight: surface it to
    # the caller as a conflict instead of silently creating a second job (see
    # find_active_job_for_submission - errored/cancelled jobs don't count, so a
    # retry after failure is never blocked). The frontend re-submits with
    # confirm_duplicate=true once the user confirms they want it anyway.
    if not confirm_duplicate:
        existing_job = await asyncio.to_thread(
            find_active_job_for_submission, clean_url, media_type, quality_value
        )
        if existing_job is not None:
            return JSONResponse(
                status_code=409,
                content={"detail": "duplicate_job", "existing_job": job_to_dict(existing_job)},
            )

    # Try to extract metadata with short timeout - don't block job creation if it fails
    meta: dict[str, object] = {"video_title": None, "video_meta_hover": None}
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
                finished_at=utc_timestamp(),
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

    cancel_worker_job(job_id_str)
    logger.info("Cancellation requested for job %s (status: %s)", job_id_str, status)

    job = await asyncio.to_thread(get_job, job_id_str)
    return job_to_dict(job)
