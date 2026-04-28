#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import asyncio
import logging
import hmac
import json
import os
import re
import shutil
import subprocess
import uuid
from hashlib import pbkdf2_hmac, sha256
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from datetime import datetime, UTC
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from .analysis_worker import (
    set_status_callback as set_analysis_status_callback,
    start_analysis_workers,
    stop_analysis_workers,
    submit_analysis,
    SubmitResult,
)
from .bpm_cluster import cluster_bpms
from .governor import governor
from .worker import get_job_queue, start_workers, set_status_callback, stop_workers, _shutdown_event
from .db import (
    close_db,
    get_job,
    get_settings,
    get_stats,
    init_db,
    insert_job,
    list_completed_bpms,
    list_jobs,
    list_jobs_requiring_audio_analysis,
    paginate_jobs,
    purge_old_jobs,
    set_settings,
    update_job,
)
from .utils.version import BUILD_INFO, VERSION
from .utils.housekeeping import cleanup_expired_jobs
from .session import (
    SESSION_COOKIE,
    SESSION_HARD_LIMIT_SECONDS,
    create_session,
    validate_session,
    renew_session,
    authenticated_user,
    set_session_cookie,
    delete_session_cookie,
)
from middleware.csrf import CSRFMiddleware

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = (BASE_DIR.parent / "data").resolve()
_COOKIES_PATH = BASE_DIR.parent / "youtube_cookies.txt"

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY")
if not _SECRET_KEY:
    raise RuntimeError("TUBEYOU_SECRET_KEY environment variable is required")

_DEV_MODE = os.environ.get("LOG_LEVEL", "").lower() == "debug"
_SESSION_COOKIE = SESSION_COOKIE  # Re-export for compatibility
_CSRF_COOKIE = "tubeyou_csrf"
_LALAL_AUTH_REQUEST_COOLDOWN_SECONDS = 30
_LALAL_AUTH_VALIDATION_CACHE_SECONDS = 300
_ALLOWED_MEDIA_TYPES = frozenset({"audio", "video"})
_ALLOWED_QUALITIES = frozenset({"max", "medium", "small"})
_WS_BROADCAST_CONCURRENCY = 50
_SKIP_RENEW_PATHS = (
    "/static",
    "/favicon.ico",
    "/health",
    "/login",
    "/logout",
    "/thumbnail",
    "/ws",
    "/download",
)

limiter = Limiter(key_func=get_remote_address)

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Jinja2 filter: Convert UTC string to local time according to TZ-Env
_tz_name = os.environ.get("TZ", "UTC")
try:
    _LOCAL_TZ = ZoneInfo(_tz_name)
except ZoneInfoNotFoundError:
    _LOCAL_TZ = ZoneInfo("UTC")


def _localtime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        local_dt = dt.astimezone(_LOCAL_TZ)
        date_part = local_dt.strftime("%d.%m.%Y")
        time_part = local_dt.strftime("%H:%M")
        return f'<span class="date-part">{date_part}</span> <span class="time-part">{time_part}</span>'
    except (ValueError, TypeError):
        return value


def _filesize(value: int | None) -> str:
    """Jinja filter: human-readable filesize."""
    if value is None:
        return "–"
    if value == 0:
        return "0 B"
    for unit, divisor in (("TB", 1_099_511_627_776), ("GB", 1_073_741_824), ("MB", 1_048_576), ("KB", 1_024)):
        if value >= divisor:
            precision = 2 if unit in ("TB", "GB") else 1
            return f"{value / divisor:.{precision}f} {unit}"
    return f"{value} B"


def _status_class(status: str | None) -> str:
    if status == "error":
        return "danger"
    if status in {"done", "analysis_done"}:
        return "success"
    if status == "analysis":
        return "primary"
    return "primary"


def _status_icon(status: str | None) -> str:
    if status in {"done", "analysis_done"}:
        return "check_circle"
    if status == "error":
        return "error"
    if status == "analysis":
        return "graphic_eq"
    return "schedule"


def _mask_api_key(value: str | None) -> str:
    if not value or not str(value).strip():
        return ""
    return "****"


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    public_settings = dict(settings)
    # Mask sensitive fields
    public_settings["lalalaai_auth_key"] = _mask_api_key(public_settings.get("lalalaai_auth_key"))
    public_settings["admin_password_hash"] = _mask_api_key(public_settings.get("admin_password_hash"))
    public_settings["lalalaai_configured"] = _is_lalal_configured(settings)
    return public_settings


def _is_lalal_configured(settings: dict[str, Any]) -> bool:
    return bool(
        str(settings.get("lalalaai_email", "")).strip()
        and str(settings.get("lalalaai_auth_key", "")).strip()
    )


async def _get_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return payload


def _require_html_auth(request: Request) -> RedirectResponse | None:
    if _current_user(request):
        return None
    return RedirectResponse(url="/login", status_code=303)


def _resolve_job_path(raw_filename: str | None) -> Path:
    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")
    file_path = Path(str(raw_filename)).resolve()
    try:
        file_path.relative_to(DATA_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    return file_path


templates.env.filters["localtime"] = _localtime
templates.env.filters["filesize"] = _filesize
templates.env.filters["status_class"] = _status_class
templates.env.filters["status_icon"] = _status_icon
templates.env.globals["now"] = datetime.now
templates.env.globals["VERSION"] = VERSION
templates.env.globals["BUILD_INFO"] = BUILD_INFO

logger = logging.getLogger(__name__)


def _derive_salt(username: str) -> bytes:
    return sha256(f"{_SECRET_KEY}:{username}:salt".encode("utf-8")).digest()


def _derive_pepper() -> str:
    return sha256(f"{_SECRET_KEY}:pepper".encode("utf-8")).hexdigest()


def _hash_password(username: str, password: str) -> str:
    peppered = f"{password}:{_derive_pepper()}".encode("utf-8")
    return pbkdf2_hmac("sha256", peppered, _derive_salt(username), 200_000).hex()


_DEFAULT_USER = os.environ.get("TUBEYOU_ADMIN_USER", "admin")
_DEFAULT_PASS = os.environ.get("TUBEYOU_ADMIN_PASSWORD")
if not _DEFAULT_PASS:
    if _DEV_MODE:
        _DEFAULT_PASS = "admin"
    else:
        raise RuntimeError("TUBEYOU_ADMIN_PASSWORD environment variable is required")

_DEFAULT_HASH = _hash_password(_DEFAULT_USER, _DEFAULT_PASS)

# YouTube URL validation regex
_YOUTUBE_URL_PATTERN = re.compile(
    r'^https?://'
    r'(?:www\.|m\.)?'
    r'(?:'
    r'youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)'
    r'|youtu\.be/'
    r')'
    r'[\w-]{11}'
    r'(?:[?#][^\s]*)?$',
    re.IGNORECASE,
)


def _validate_youtube_url(url: str) -> tuple[bool, str]:
    """
    Validate that a URL is a valid YouTube video URL.
    Returns (is_valid, error_message).
    """
    if not url or not isinstance(url, str):
        return False, "URL is required"
    
    url = url.strip()
    
    # Normalize common copy/paste issues from mobile apps / HTML sources
    url = url.replace("&amp;", "&")
    url = re.sub(r"[\u200B-\u200D\uFEFF]", "", url)  # remove zero-width chars
    
    if not url:
        return False, "URL is required"
    
    # Check URL length (prevent DoS with extremely long URLs)
    if len(url) > 2048:
        return False, "URL is too long"
    
    # Must start with http:// or https://
    if not url.startswith(('http://', 'https://')):
        return False, "URL must start with http:// or https://"

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL"

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    def _clean_video_id(value: str) -> str:
        """Strip zero-width chars and non-ID characters from video ID."""
        cleaned = re.sub(r"[\u200B-\u200D\uFEFF]", "", value.strip())
        return re.sub(r"[^\w-]", "", cleaned)

    def _is_video_id(value: str) -> bool:
        return bool(re.fullmatch(r"[\w-]{11}", value))

    # Check if it's a YouTube video URL. Allow extra query parameters such as
    # playlist context (list/start_radio), but require a real video ID.
    if host.endswith("youtu.be"):
        segment = _clean_video_id(path.strip("/").split("/")[0])
        if _is_video_id(segment):
            return True, ""
    elif host.endswith("youtube.com"):
        if path == "/watch":
            params = parse_qs(parsed.query, keep_blank_values=False)
            video_id = _clean_video_id((params.get("v") or [""])[0])
            if _is_video_id(video_id):
                return True, ""
        elif path.startswith(("/shorts/", "/embed/", "/v/")):
            segment = _clean_video_id(path.strip("/").split("/")[1]) if "/" in path.strip("/") else ""
            if _is_video_id(segment):
                return True, ""

    return False, "Invalid YouTube URL. Supported formats: youtube.com/watch?v=..., youtu.be/..., youtube.com/shorts/..."


def _normalize_info_url(url: str) -> str:
    """Normalize a YouTube URL to a single-video URL for metadata extraction.

    This removes playlist context (e.g. list/index) that can trigger slower
    resolution paths and intermittent timeouts in yt-dlp.
    """
    value = url.strip()
    try:
        parsed = urlparse(value)
    except Exception:
        return value

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host.endswith("youtube.com") and path == "/watch":
        params = parse_qs(parsed.query, keep_blank_values=False)
        video_id = (params.get("v") or [""])[0].strip()
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return value

    if host.endswith("youtu.be"):
        segment = path.strip("/").split("/")[0].strip()
        if segment:
            return f"https://youtu.be/{segment}"

    return value


def _load_video_info(url: str) -> dict[str, Any] | None:
    info: dict[str, Any] | None = None
    
    # Build yt-dlp options with optional cookies for age-restricted content
    ydl_opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if _COOKIES_PATH.is_file():
        ydl_opts["cookiefile"] = str(_COOKIES_PATH)
    
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            extracted = ydl.extract_info(url, download=False)
        if isinstance(extracted, dict):
            info = extracted
    except Exception as exc:
        logger.debug("yt-dlp library extraction failed for %s: %s", url, exc)
        info = None

    if info is None:
        try:
            cmd = ["yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json"]
            if _COOKIES_PATH.is_file():
                cmd.extend(["--cookies", str(_COOKIES_PATH)])
            cmd.append(url)
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            parsed = json.loads(result.stdout or "{}")
            if isinstance(parsed, dict):
                info = parsed
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp subprocess timed out for %s", url)
            return None
        except FileNotFoundError:
            logger.error("yt-dlp command not found in PATH")
            return None
        except Exception as exc:
            logger.debug("yt-dlp subprocess extraction failed for %s: %s", url, exc)
            return None

    return info


def _extract_video_meta(url: str) -> dict[str, object]:
    """Best-effort metadata extraction for title + hover details."""
    info = _load_video_info(url)
    if info is None:
        return {"video_title": None, "video_meta_hover": None}

    title = str(info.get("title") or "").strip()
    channel = str(info.get("channel") or "").strip()
    uploader = str(info.get("uploader") or "").strip()
    duration = info.get("duration")
    views = info.get("view_count")

    lines: list[str] = []
    if channel:
        lines.append(f"Channel: {channel}")
    if uploader:
        lines.append(f"Uploader: {uploader}")
    if isinstance(duration, int) and duration > 0:
        mins, secs = divmod(duration, 60)
        lines.append(f"Duration: {mins}:{secs:02d}")
    if isinstance(views, int) and views >= 0:
        lines.append(f"Views: {views:,}")

    return {
        "video_title": title or None,
        "video_meta_hover": " | ".join(lines) if lines else None,
    }


def _verify_login(username: str, password: str) -> bool:
    stored_hash = _DEFAULT_HASH

    # Check if a custom password hash is stored in settings.
    try:
        settings = get_settings()
        custom_hash = settings.get("admin_password_hash")
        if custom_hash and str(custom_hash).strip():
            stored_hash = str(custom_hash).strip()
    except Exception:
        logger.warning("Could not load password hash from settings; falling back to default", exc_info=True)

    password_ok = hmac.compare_digest(_hash_password(_DEFAULT_USER, password), stored_hash)
    username_ok = hmac.compare_digest(username, _DEFAULT_USER)
    return username_ok and password_ok


def _current_user(request: Request) -> str | None:
    """Get current authenticated user from request."""
    return authenticated_user(request.cookies.get(_SESSION_COOKIE))


def require_user(request: Request) -> str:
    """Dependency: require authenticated user."""
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_session(request: Request) -> str:
    """Dependency: require a valid authenticated session."""
    user = validate_session(request.cookies.get(_SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_user_json(request: Request) -> str:
    """Dependency: require authenticated user (JSON response)."""
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def _job_to_dict(job: Any) -> dict[str, object]:
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


def _empty_info_payload() -> dict[str, object]:
    """Return the canonical fallback payload for `/api/info` failures."""
    return {
        "title": None,
        "channel": None,
        "uploader": None,
        "duration": None,
        "view_count": None,
        "formats": [],
        "unavailable": True,
    }


connections: set[WebSocket] = set()
_EVENT_QUEUE_MAXSIZE = 10_000
event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)

_STATS_CACHE_TTL_SECONDS = 60.0
_stats_cache: dict[str, Any] = {"data": None, "ts": 0.0}

_HOUSEKEEPING_INTERVAL: int = 3600  # Every hour


def _queue_event(payload: dict[str, Any]) -> None:
    """Enqueue a status payload or drop it when the bounded queue is full."""
    try:
        event_queue.put_nowait(payload)
    except asyncio.QueueFull:
        logger.warning("Status event dropped because the event queue is full")


async def _get_cached_stats() -> dict[str, int]:
    """Return dashboard stats from a short TTL cache to avoid repeated scans."""
    now_ts = time()
    cached = _stats_cache.get("data")
    cached_ts = float(_stats_cache.get("ts", 0.0) or 0.0)
    if cached is not None and (now_ts - cached_ts) < _STATS_CACHE_TTL_SECONDS:
        return cached

    stats = await asyncio.to_thread(get_stats)
    _stats_cache["data"] = stats
    _stats_cache["ts"] = now_ts
    return stats


async def _housekeeping_daemon() -> None:
    log = logging.getLogger("tubeyou.housekeeping")
    while True:
        await asyncio.sleep(_HOUSEKEEPING_INTERVAL)
        try:
            # Get retention days from settings (default 7)
            settings = await asyncio.to_thread(get_settings)
            keep_days = settings.get("retention_days", 7)
            
            # Cleanup expired jobs (DB + filesystem)
            await asyncio.to_thread(
                cleanup_expired_jobs,
                keep_days,
                DATA_DIR,
                purge_old_jobs,
            )
        except Exception as exc:
            log.warning("Housekeeping failed: %s", exc)


def _check_dependencies() -> None:
    """
    Check that required external tools are available.
    Raises RuntimeError if any dependency is missing.
    """
    missing = []
    for cmd in ("yt-dlp", "ffmpeg"):
        if shutil.which(cmd) is None:
            missing.append(cmd)
    
    if missing:
        msg = f"Missing required dependencies: {', '.join(missing)}"
        logger.critical("FATAL: %s", msg)
        logger.critical("Install dependencies with: sudo apt-get install ffmpeg")
        logger.critical("Install yt-dlp with: pip install yt-dlp")
        logger.critical(
            "Alternative install: sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && sudo chmod +x /usr/local/bin/yt-dlp"
        )
        raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_dependencies()
    init_db()
    
    # Configure Governor for auto-detection of resources (Docker cgroup-aware)
    await asyncio.to_thread(governor.configure)

    loop = asyncio.get_running_loop()

    def _thread_status_callback(payload: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(_queue_event, payload)

    set_status_callback(_thread_status_callback)
    set_analysis_status_callback(_thread_status_callback)
    start_workers()  # Auto-detect worker count via Governor
    start_analysis_workers()

    pending_analysis_jobs = await asyncio.to_thread(list_jobs_requiring_audio_analysis)
    for row in pending_analysis_jobs:
        raw_filename = str(row["filename"] or "").strip()
        if not raw_filename:
            continue
        result = submit_analysis(
            str(row["id"]),
            Path(raw_filename),
            duration_seconds=int(row["duration_seconds"] or 0) or None,
            block=False,
        )
        if result is not SubmitResult.QUEUED:
            msg = (
                "Finished (audio analysis unavailable during shutdown)"
                if result is SubmitResult.REJECTED_SHUTDOWN
                else "Finished (audio analysis backlog full)"
            )
            await asyncio.to_thread(
                update_job,
                str(row["id"]),
                status="done",
                message=msg,
            )

    broadcaster_task = asyncio.create_task(_event_broadcaster())
    housekeeping_task = asyncio.create_task(_housekeeping_daemon())

    try:
        yield
    finally:
        _shutdown_event.set()
        for task in (broadcaster_task, housekeeping_task):
            task.cancel()
        for task in (broadcaster_task, housekeeping_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("Background task exited during shutdown: %s", exc)

        for ws in list(connections):
            try:
                await ws.close()
            except Exception:
                pass
        connections.clear()

        stop_analysis_workers(timeout=30.0)
        stop_workers(timeout=2.0)
        close_db()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class SessionRenewalMiddleware(BaseHTTPMiddleware):
    """Middleware to renew session cookie on every authenticated request (sliding window)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Only renew for successful responses and non-static/resource paths.
        if response.status_code >= 400:
            return response

        request_path = request.url.path
        if any(request_path == prefix or request_path.startswith(f"{prefix}/") for prefix in _SKIP_RENEW_PATHS):
            return response

        old_token = request.cookies.get(_SESSION_COOKIE)
        if old_token:
            new_token = renew_session(old_token)
            if new_token and new_token != old_token:
                set_session_cookie(response, new_token, request)
        return response


app.add_middleware(SessionRenewalMiddleware)
app.add_middleware(
    CSRFMiddleware,
    csrf_cookie_name=_CSRF_COOKIE,
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    _ = (request, exc)
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


async def _broadcast_payload(payload: dict[str, Any], sockets: list[WebSocket]) -> None:
    queue: asyncio.Queue[WebSocket] = asyncio.Queue()
    for ws in sockets:
        queue.put_nowait(ws)

    workers = min(_WS_BROADCAST_CONCURRENCY, len(sockets))

    async def _sender() -> None:
        while True:
            try:
                ws = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await _send_safe(ws, payload)
            finally:
                queue.task_done()

    await asyncio.gather(*(_sender() for _ in range(workers)))


async def _event_broadcaster():
    while True:
        payload = await event_queue.get()
        if not connections:
            continue

        sockets = list(connections)
        await _broadcast_payload(payload, sockets)


async def _send_safe(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(ws.send_json(payload), timeout=5.0)
    except Exception:
        connections.discard(ws)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    redirect = _require_html_auth(request)
    if redirect:
        return redirect
    jobs = await asyncio.to_thread(paginate_jobs, limit=50, offset=0)
    stats = await _get_cached_stats()
    settings = await asyncio.to_thread(get_settings)
    lalal_enabled = _is_lalal_configured(settings)
    return templates.TemplateResponse(request=request, name="index.html", context={
        "jobs": jobs,
        "stats": stats,
        "lalal_enabled": lalal_enabled,
    })


@app.get("/api/jobs")
@limiter.limit("60/minute")
async def api_jobs(request: Request, _user: str = Depends(require_user), offset: int = 0, limit: int = 50):
    _ = request

    safe_offset = max(0, offset)
    safe_limit = min(max(1, limit), 100)

    jobs = await asyncio.to_thread(paginate_jobs, limit=safe_limit, offset=safe_offset)
    return [_job_to_dict(job) for job in jobs]


@app.get("/api/stats/bpm-clusters")
@limiter.limit("30/minute")
async def api_bpm_clusters(request: Request, _user: str = Depends(require_user), limit: int = 1000):
    _ = request
    safe_limit = min(max(1, limit), 5000)
    bpms = await asyncio.to_thread(list_completed_bpms, safe_limit)
    clusters = cluster_bpms(bpms)
    return [
        {"bpm": bpm_bucket, "count": count}
        for bpm_bucket, count in clusters
    ]


@app.get("/api/info")
@limiter.limit("10/minute")
async def api_info(request: Request, url: str, user: str = Depends(require_user)):
    """Extract video metadata using yt-dlp."""
    _ = request
    is_valid, error_msg = _validate_youtube_url(url)
    if not is_valid:
        return JSONResponse(status_code=400, content={"detail": error_msg})

    try:
        info_url = _normalize_info_url(url)
        info = await asyncio.wait_for(asyncio.to_thread(_load_video_info, info_url), timeout=20.0)

        if not info:
            return _empty_info_payload()

        # Extract format qualities
        formats = set()
        for fmt in info.get("formats", []):
            note = fmt.get("format_note")
            if note:
                formats.add(note)
        
        return {
            "title": info.get("title"),
            "channel": info.get("channel"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "view_count": info.get("view_count"),
            "formats": sorted(list(formats))[:5],
        }
    except asyncio.TimeoutError:
        logger.warning("Video info extraction timed out for URL: %s", url)
        return _empty_info_payload()
    except Exception as exc:
        logger.warning("Video info extraction failed for URL %s: %s", url, exc)
        return _empty_info_payload()


@app.get("/api/settings")
@limiter.limit("30/minute")
async def api_get_settings(request: Request, _user: str = Depends(require_user)):
    """Get all settings."""
    _ = request
    settings = await asyncio.to_thread(get_settings)
    return _public_settings(settings)


@app.post("/api/settings")
@limiter.limit("10/minute")
async def api_set_settings(request: Request, _user: str = Depends(require_session)):
    """Update retention/session settings and optional admin password."""
    payload = await _get_json(request)
    
    # Validate inputs
    settings_to_update: dict[str, Any] = {}

    if "retention_days" in payload:
        try:
            retention_days = int(payload["retention_days"])
            if retention_days < 1 or retention_days > 365:
                raise ValueError("retention_days must be between 1 and 365")
            settings_to_update["retention_days"] = retention_days
        except (ValueError, TypeError) as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
    
    if "session_idle_minutes" in payload:
        try:
            session_idle = int(payload["session_idle_minutes"])
            if session_idle < 5 or session_idle > 1440:
                raise ValueError("session_idle_minutes must be between 5 and 1440")
            settings_to_update["session_idle_minutes"] = session_idle
        except (ValueError, TypeError) as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
    
    # Update settings
    try:
        if "admin_password" in payload and payload["admin_password"]:
            new_password = str(payload["admin_password"]).strip()
            if len(new_password) < 8:
                return JSONResponse(status_code=400, content={"detail": "Password must be at least 8 characters"})
            current_password = str(payload.get("current_password", ""))
            is_valid_current_password = await asyncio.to_thread(_verify_login, _DEFAULT_USER, current_password)
            if not is_valid_current_password:
                return JSONResponse(status_code=403, content={"detail": "Current password is invalid"})
            # Hash the new password
            settings_to_update["admin_password_hash"] = await asyncio.to_thread(
                _hash_password,
                _DEFAULT_USER,
                new_password,
            )
        
        if settings_to_update:
            await asyncio.to_thread(set_settings, settings_to_update, allow_internal=True)
        
        return {"ok": True, "message": "Settings updated"}
    except Exception:
        logger.exception("Settings update failed")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/api/lalal/status")
@limiter.limit("30/minute")
async def api_lalal_status(request: Request, force_refresh: bool = False, _user: str = Depends(require_user)):
    """Return saved Lalal.ai auth status and validation state."""
    _ = request
    settings = await asyncio.to_thread(get_settings)
    email = str(settings.get("lalalaai_email", "")).strip()
    auth_key = str(settings.get("lalalaai_auth_key", "")).strip()

    if not _is_lalal_configured(settings):
        return {
            "ok": True,
            "configured": False,
            "email": "",
            "token_valid": False,
            "validation_error": "",
            "validated_at": 0,
        }

    now_ts = int(time())
    checked_at = int(settings.get("lalalaai_auth_checked_at", 0) or 0)
    token_valid = bool(settings.get("lalalaai_auth_is_valid", False))
    validation_error = str(settings.get("lalalaai_auth_last_error", "") or "").strip()

    should_validate = (
        force_refresh
        or checked_at <= 0
        or (now_ts - checked_at) >= _LALAL_AUTH_VALIDATION_CACHE_SECONDS
    )

    if should_validate:
        from .lalal import LalalClient

        token_valid = False
        validation_error = ""
        client = LalalClient(auth_key)
        try:
            await asyncio.wait_for(client.check_quota(), timeout=20.0)
            token_valid = True
        except Exception as exc:
            token_valid = False
            validation_error = str(exc)
        finally:
            await client.close()

        checked_at = now_ts
        await asyncio.to_thread(
            set_settings,
            {
                "lalalaai_auth_checked_at": checked_at,
                "lalalaai_auth_is_valid": token_valid,
                "lalalaai_auth_last_error": validation_error,
            },
        )

    return {
        "ok": True,
        "configured": True,
        "email": email,
        "token_valid": token_valid,
        "validation_error": validation_error,
        "validated_at": checked_at,
    }


@app.post("/api/lalal/auth/request")
@limiter.limit("5/minute")
async def api_lalal_auth_request(request: Request, _user: str = Depends(require_user)):
    """Request OTP email via Lalal website auth flow."""

    payload = await _get_json(request)

    email = str(payload.get("email", "")).strip().lower()
    google_cid = str(payload.get("google_cid", "")).strip()

    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"detail": "Valid email address is required"})

    settings = await asyncio.to_thread(get_settings)
    now_ts = int(time())
    last_requested_at = int(settings.get("lalalaai_auth_requested_at", 0) or 0)
    remaining_seconds = _LALAL_AUTH_REQUEST_COOLDOWN_SECONDS - (now_ts - last_requested_at)

    if remaining_seconds > 0:
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Please wait {remaining_seconds} seconds before requesting another token.",
                "retry_after_seconds": remaining_seconds,
            },
        )

    from .lalal import LalalOtpAuthClient

    try:
        async with LalalOtpAuthClient(timeout=30.0) as auth_client:
            await auth_client.request_otp(
                email=email,
                google_cid=google_cid,
            )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"Failed to request email code: {exc}"})

    await asyncio.to_thread(set_settings, {"lalalaai_auth_requested_at": now_ts})

    return {"ok": True, "message": "Verification email sent. Please enter your 6-digit code."}


@app.get("/api/lalal/auth/cooldown")
@limiter.limit("30/minute")
async def api_lalal_auth_cooldown(request: Request, _user: str = Depends(require_user)):
    """Return remaining cooldown seconds for token requests."""
    _ = request
    settings = await asyncio.to_thread(get_settings)
    now_ts = int(time())
    last_requested_at = int(settings.get("lalalaai_auth_requested_at", 0) or 0)
    remaining_seconds = max(0, _LALAL_AUTH_REQUEST_COOLDOWN_SECONDS - (now_ts - last_requested_at))

    return {
        "ok": True,
        "remaining_seconds": remaining_seconds,
        "cooldown_seconds": _LALAL_AUTH_REQUEST_COOLDOWN_SECONDS,
    }


@app.post("/api/lalal/auth/verify")
@limiter.limit("10/minute")
async def api_lalal_auth_verify(request: Request, _user: str = Depends(require_user)):
    """Verify 6-digit OTP code and store Lalal activation key."""

    payload = await _get_json(request)

    email = str(payload.get("email", "")).strip().lower()
    code = str(payload.get("code", "")).strip()

    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"detail": "Valid email address is required"})
    if not code or len(code) != 6 or not code.isdigit():
        return JSONResponse(status_code=400, content={"detail": "Invalid 6-digit code"})

    from .lalal import LalalClient, LalalOtpAuthClient

    try:
        async with LalalOtpAuthClient(timeout=30.0) as auth_client:
            activation_key = await auth_client.exchange_code_for_activation_key(email=email, code=code)

        client = LalalClient(activation_key)
        try:
            await asyncio.wait_for(client.check_quota(), timeout=20.0)
        finally:
            await client.close()
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"Authentication failed: {exc}"})

    # Store auth credentials
    await asyncio.to_thread(
        set_settings,
        {
            "lalalaai_email": email,
            "lalalaai_auth_key": activation_key,
            "lalalaai_auth_checked_at": int(time()),
            "lalalaai_auth_is_valid": True,
            "lalalaai_auth_last_error": "",
        },
    )

    return {"ok": True, "message": "Authentication successful"}


@app.post("/api/lalal/auth/activation-key")
@limiter.limit("10/minute")
async def api_lalal_auth_activation_key(request: Request, _user: str = Depends(require_user)):
    """Validate and store a manually provided Lalal activation key."""

    payload = await _get_json(request)

    email = str(payload.get("email", "")).strip().lower()
    activation_key = str(payload.get("activation_key", "")).strip()

    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"detail": "Valid email address is required"})
    if not activation_key:
        return JSONResponse(status_code=400, content={"detail": "Activation key is required"})

    from .lalal import LalalClient

    client = LalalClient(activation_key)
    try:
        await asyncio.wait_for(client.check_quota(), timeout=20.0)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"Invalid activation key: {exc}"})
    finally:
        await client.close()

    await asyncio.to_thread(
        set_settings,
        {
            "lalalaai_email": email,
            "lalalaai_auth_key": activation_key,
            "lalalaai_auth_checked_at": int(time()),
            "lalalaai_auth_is_valid": True,
            "lalalaai_auth_last_error": "",
        },
    )

    return {"ok": True, "message": "Activation key saved"}


@app.post("/api/lalal/auth/logout")
@limiter.limit("20/minute")
async def api_lalal_auth_logout(request: Request, _user: str = Depends(require_user)):
    """Clear Lalal.ai auth credentials."""
    _ = request
    await asyncio.to_thread(
        set_settings,
        {
            "lalalaai_email": "",
            "lalalaai_auth_key": "",
            "lalalaai_auth_checked_at": 0,
            "lalalaai_auth_is_valid": False,
            "lalalaai_auth_last_error": "",
        },
    )

    return {"ok": True, "message": "Logged out"}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    redirect = _require_html_auth(request)
    if redirect:
        return redirect

    settings = await asyncio.to_thread(get_settings)
    lalal_configured = _is_lalal_configured(settings)
    lalal_email = str(settings.get("lalalaai_email", "")).strip()
    
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": settings,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "lalal_status": "Connected" if lalal_configured else "Not configured",
            "lalal_configured": lalal_configured,
            "lalal_email": lalal_email,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = _current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": getattr(request.state, "csrf_token", "")},
    )


@app.post("/login")
@limiter.limit("10/minute")
async def login(request: Request):
    payload = await _get_json(request)
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if not await asyncio.to_thread(_verify_login, username, password):
        return JSONResponse(status_code=401, content={"ok": False, "detail": "Invalid username or password"})

    resp = JSONResponse(content={"ok": True, "redirect": "/"})
    set_session_cookie(resp, create_session(username), request)
    return resp


@app.post("/logout")
def logout(request: Request):
    resp = JSONResponse(content={"ok": True})
    delete_session_cookie(resp, request)
    return resp


@app.get("/logout")
def logout_page(request: Request):
    """JS-independent logout fallback via navigation."""
    resp = RedirectResponse(url="/login", status_code=303)
    delete_session_cookie(resp, request)
    return resp


@app.post("/api/submit")
@limiter.limit("5/minute")
async def api_submit(
    request: Request,
    url: str = Form(...),
    media_type: str = Form(..., alias="type"),
    quality: str = Form(...),
    _user: str = Depends(require_user),
):
    _ = request
    media_type = str(media_type).strip().lower()
    quality_value = str(quality).strip().lower()

    if media_type not in _ALLOWED_MEDIA_TYPES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid type. Allowed: {', '.join(sorted(_ALLOWED_MEDIA_TYPES))}"},
        )
    if quality_value not in _ALLOWED_QUALITIES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid quality. Allowed: {', '.join(sorted(_ALLOWED_QUALITIES))}"},
        )
    
    # Validate YouTube URL
    is_valid, error_msg = _validate_youtube_url(url)
    if not is_valid:
        return JSONResponse(status_code=400, content={"detail": error_msg})

    # Try to extract metadata with short timeout - don't block job creation if it fails
    meta: dict[str, object] = {"video_title": None, "video_meta_hover": None}
    try:
        meta = await asyncio.wait_for(
            asyncio.to_thread(_extract_video_meta, url.strip()),
            timeout=8.0,
        )
    except Exception as exc:
        logger.debug("Metadata extraction skipped for submit (will be fetched by worker): %s", exc)
    
    job_id = str(uuid.uuid4())
    clean_url = url.strip()
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
    await asyncio.to_thread(get_job_queue().put, (job_id, clean_url, media_type, quality_value))
    job = await asyncio.to_thread(get_job, job_id)
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/cancel")
@limiter.limit("20/minute")
async def cancel_job(request: Request, job_id: uuid.UUID, _user: str = Depends(require_user_json)):
    """Cancel a running or queued job."""

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    
    status = job["status"] or ""
    if status in ("done", "analysis", "analysis_done", "error", "cancelled"):
        return JSONResponse(status_code=400, content={"error": f"Cannot cancel job with status: {status}"})
    
    # Mark job for cancellation
    from . import worker
    worker.cancel_job(job_id)
    
    # Update status to "cancelled" if currently queued, else let worker handle it
    if status == "queued":
        await asyncio.to_thread(
            update_job,
            job_id_str,
            status="cancelled",
            message="Cancelled by user",
            finished_at=datetime.now(UTC).isoformat(),
        )
    
    logger.info("Cancellation requested for job %s (status: %s)", job_id_str, status)
    
    job = await asyncio.to_thread(get_job, job_id_str)
    return _job_to_dict(job)


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: uuid.UUID):
    redirect = _require_html_auth(request)
    if redirect:
        return redirect
    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        return templates.TemplateResponse(
            request=request,
            name="job.html",
            context={"job": None, "job_id": job_id_str},
        )

    return templates.TemplateResponse(
        request=request,
        name="job.html",
        context={"job": job},
    )


@app.get("/download/{job_id}")
@limiter.limit("60/minute")
async def download(request: Request, job_id: uuid.UUID):
    if not _current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    job = await asyncio.to_thread(get_job, str(job_id))
    if not job or job["status"] not in {"done", "analysis", "analysis_done"}:
        return JSONResponse(status_code=404, content={"error": "not ready"})

    raw_filename = job["filename"]
    if not raw_filename:
        return JSONResponse(status_code=404, content={"error": "not ready"})

    try:
        file_path = _resolve_job_path(raw_filename)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    if not file_path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})

    return FileResponse(path=file_path, filename=file_path.name)


@app.post("/api/lalal/{job_id}")
@limiter.limit("5/minute")
async def lalal_split(request: Request, job_id: uuid.UUID, stem: str = "vocals", _user: str = Depends(require_user_json)):
    """Split audio using Lalal.ai API."""

    # Validate stem type
    if stem not in ("vocals", "instrumental"):
        return JSONResponse(status_code=400, content={"error": "Invalid stem type. Use 'vocals' or 'instrumental'"})

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})

    if job["status"] != "done":
        return JSONResponse(status_code=400, content={"error": "Job not ready"})

    if job["type"] != "audio":
        return JSONResponse(status_code=400, content={"error": "Lalal.ai only works with audio jobs"})

    raw_filename = job["filename"]
    try:
        file_path = _resolve_job_path(raw_filename)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    if not file_path.is_file():
        return JSONResponse(status_code=404, content={"error": "Source file not found"})

    output_dir = DATA_DIR / job_id_str
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = file_path.stem

    vocals_path = output_dir / f"{base_name}_vocals.mp3"
    instrumental_path = output_dir / f"{base_name}_instrumental.mp3"

    # Cache short-circuit only when both Lalal stems exist locally.
    if vocals_path.is_file() and instrumental_path.is_file():
        cached_path = vocals_path if stem == "vocals" else instrumental_path
        return JSONResponse(content={
            "ok": True,
            "cached": True,
            "download_url": f"/api/lalal/download/{job_id_str}?stem={stem}",
            "filename": cached_path.name,
        })

    # Import lalal module
    try:
        from .lalal import get_lalal_client, StemType, LalalError
    except ImportError:
        return JSONResponse(status_code=500, content={"error": "Lalal.ai module not available"})

    client = get_lalal_client()
    if not client:
        return JSONResponse(status_code=400, content={"error": "Lalal.ai API key not configured"})

    loop = asyncio.get_running_loop()

    def sync_progress_callback(stage: str, pct: int) -> None:
        payload = {
            "type": "lalal_progress",
            "job_id": job_id,
            "stem": stem,
            "stage": stage,
            "progress": pct,
        }
        loop.call_soon_threadsafe(_queue_event, payload)

    try:
        # Process with Lalal.ai
        stem_type = StemType.VOCALS
        # Lalal always returns both tracks for this split - download both once
        # so future clicks use cached files without reprocessing.
        download_stem = True
        download_backing = True

        results = await client.process_file(
            file_path,
            output_dir,
            stem=stem_type,
            download_stem=download_stem,
            download_backing=download_backing,
            progress_callback=sync_progress_callback,
        )

        # Return the path to download
        if stem == "vocals" and "stem" in results:
            result_path = results["stem"]
        elif stem == "instrumental" and "backing" in results:
            result_path = results["backing"]
        else:
            return JSONResponse(status_code=500, content={"error": "Processing completed but no output file"})

        # Return download link
        return JSONResponse(content={
            "ok": True,
            "download_url": f"/api/lalal/download/{job_id_str}?stem={stem}",
            "filename": result_path.name,
        })

    except LalalError as e:
        logger.error("Lalal.ai error for job %s: %s", job_id, e)
        return JSONResponse(status_code=500, content={"error": str(e)})
    except Exception as e:
        logger.exception("Unexpected error in Lalal.ai processing for job %s", job_id)
        return JSONResponse(status_code=500, content={"error": f"Processing failed: {e}"})
    finally:
        try:
            await client.close()
        except Exception:
            logger.debug("Could not close Lalal client cleanly", exc_info=True)


@app.get("/api/lalal/download/{job_id}")
async def lalal_download(
    request: Request,
    job_id: uuid.UUID,
    stem: str = "vocals",
    _user: str = Depends(require_user_json),
):
    """Download processed Lalal.ai stem file."""
    _ = request

    if stem not in ("vocals", "instrumental"):
        return JSONResponse(status_code=400, content={"error": "Invalid stem type"})

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})

    raw_filename = job["filename"]
    try:
        source_path = _resolve_job_path(raw_filename)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    output_dir = DATA_DIR / job_id_str
    base_name = source_path.stem

    # Find the stem file
    if stem == "vocals":
        stem_path = output_dir / f"{base_name}_vocals.mp3"
    else:
        stem_path = output_dir / f"{base_name}_instrumental.mp3"

    if not stem_path.is_file():
        return JSONResponse(status_code=404, content={"error": f"{stem.capitalize()} file not found. Please process with Lalal.ai first."})

    return FileResponse(path=stem_path, filename=stem_path.name)


@app.get("/favicon.ico")
def favicon():
    """Serve favicon from static/img if exists, else 204."""
    ico_path = BASE_DIR / "static" / "img" / "favicon.ico"
    if ico_path.is_file():
        return FileResponse(path=ico_path, media_type="image/x-icon")
    # No favicon - return 204 No Content (must have empty body)
    return Response(status_code=204)


@app.get("/thumbnail/{job_id}")
@limiter.limit("60/minute")
def thumbnail(request: Request, job_id: uuid.UUID):
    """Serve cached thumbnail for a job."""
    if not _current_user(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    thumb_path = DATA_DIR / str(job_id) / "thumbnail.jpg"
    
    if not thumb_path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    
    return FileResponse(path=thumb_path, media_type="image/jpeg")


_WS_PING_INTERVAL = 30.0  # Seconds between heartbeat pings
_WS_PONG_TIMEOUT = 10.0   # Max wait for pong response


@app.websocket("/ws")
async def ws_status(websocket: WebSocket):
    """WebSocket endpoint for real-time job status updates.

    Implements ping/pong heartbeat to detect stale connections.
    Clients should respond to 'ping' messages with 'pong'.
    """
    if not authenticated_user(websocket.cookies.get(_SESSION_COOKIE)):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    connections.add(websocket)

    last_pong = asyncio.get_running_loop().time()
    ping_task: asyncio.Task[None] | None = None

    async def _heartbeat() -> None:
        nonlocal last_pong
        while True:
            await asyncio.sleep(_WS_PING_INTERVAL)
            now = asyncio.get_running_loop().time()
            # Check if client responded to last ping
            if now - last_pong > _WS_PING_INTERVAL + _WS_PONG_TIMEOUT:
                logger.debug("WebSocket client idle timeout, closing connection")
                try:
                    await websocket.close(code=1000, reason="idle timeout")
                except Exception:
                    pass
                return
            # Send ping
            try:
                await websocket.send_text("ping")
            except Exception:
                return

    try:
        ping_task = asyncio.create_task(_heartbeat())

        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=_WS_PING_INTERVAL + _WS_PONG_TIMEOUT + 5.0,
                )
                # Update last activity on any message (pong, subscribe, etc.)
                last_pong = asyncio.get_running_loop().time()
                # Client can send 'pong' explicitly or any other message counts as alive
                if message == "pong":
                    logger.debug("WebSocket pong received")
            except asyncio.TimeoutError:
                logger.debug("WebSocket receive timeout, closing")
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        connections.discard(websocket)
