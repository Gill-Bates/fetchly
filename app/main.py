#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import base64
import logging
import hmac
import json
import os
import secrets
import re
import shutil
import subprocess
import sys
import uuid
from hashlib import pbkdf2_hmac, sha256
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from datetime import datetime, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

from .worker import job_queue, start_workers, set_status_callback, stop_workers, _shutdown_event
from .db import init_db, close_db, insert_job, get_job, list_jobs, paginate_jobs, purge_old_jobs, get_stats, get_settings, set_settings
from middleware.csrf import CSRFMiddleware

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = (BASE_DIR.parent / "data").resolve()

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY")
if not _SECRET_KEY:
    raise RuntimeError("TUBEYOU_SECRET_KEY environment variable is required")

_DEV_MODE = os.environ.get("LOG_LEVEL", "").lower() == "debug"
_SESSION_COOKIE = "tubeyou_session"
_CSRF_COOKIE = "tubeyou_csrf"
_SESSION_MAX_AGE = 12 * 60 * 60

app.add_middleware(
    CSRFMiddleware,
    secret_key=_SECRET_KEY,
    csrf_cookie_name=_CSRF_COOKIE,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

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
        dt = datetime.fromisoformat(value).replace(tzinfo=UTC)
        return dt.astimezone(_LOCAL_TZ).strftime("%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return value


def _filesize(value: int | None) -> str:
    """Jinja filter: human-readable filesize."""
    if not value:
        return "-"
    for unit, divisor in (("GB", 1_073_741_824), ("MB", 1_048_576), ("KB", 1_024)):
        if value >= divisor:
            precision = 2 if unit == "GB" else 1
            return f"{value / divisor:.{precision}f} {unit}"
    return f"{value} B"


def _status_class(status: str | None) -> str:
    if status == "error":
        return "danger"
    if status == "done":
        return "success"
    return "primary"


def _status_icon(status: str | None) -> str:
    if status == "done":
        return "check_circle"
    if status == "error":
        return "error"
    return "schedule"


templates.env.filters["localtime"] = _localtime
templates.env.filters["filesize"] = _filesize
templates.env.filters["status_class"] = _status_class
templates.env.filters["status_icon"] = _status_icon

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
    r'youtube\.com/(?:watch\?.*v=|embed/|v/|shorts/)'
    r'|youtu\.be/'
    r')'
    r'[\w-]{11}'
    r'(?:[?&].*)?$',
    re.IGNORECASE
)


def _validate_youtube_url(url: str) -> tuple[bool, str]:
    """
    Validate that a URL is a valid YouTube video URL.
    Returns (is_valid, error_message).
    """
    if not url or not isinstance(url, str):
        return False, "URL is required"
    
    url = url.strip()
    
    if not url:
        return False, "URL is required"
    
    # Check URL length (prevent DoS with extremely long URLs)
    if len(url) > 2048:
        return False, "URL is too long"
    
    # Must start with http:// or https://
    if not url.startswith(('http://', 'https://')):
        return False, "URL must start with http:// or https://"
    
    # Check if it's a YouTube URL
    if not _YOUTUBE_URL_PATTERN.match(url):
        return False, "Invalid YouTube URL. Supported formats: youtube.com/watch?v=..., youtu.be/..., youtube.com/shorts/..."
    
    return True, ""


def _load_video_info(url: str) -> dict[str, Any] | None:
    info: dict[str, Any] | None = None
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            extracted = ydl.extract_info(url, download=False)
        if isinstance(extracted, dict):
            info = extracted
    except Exception:
        info = None

    if info is None:
        try:
            result = subprocess.run(
                ["yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json", url],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            parsed = json.loads(result.stdout or "{}")
            if isinstance(parsed, dict):
                info = parsed
        except Exception:
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
        lines.append(f"Kanal: {channel}")
    if uploader:
        lines.append(f"Uploader: {uploader}")
    if isinstance(duration, int) and duration > 0:
        mins, secs = divmod(duration, 60)
        lines.append(f"Dauer: {mins}:{secs:02d}")
    if isinstance(views, int) and views >= 0:
        lines.append(f"Views: {views:,}")

    return {
        "video_title": title or None,
        "video_meta_hover": " | ".join(lines) if lines else None,
    }


def _verify_login(username: str, password: str) -> bool:
    if username != _DEFAULT_USER:
        return False
    return hmac.compare_digest(_hash_password(username, password), _DEFAULT_HASH)


def _sign_session(username: str) -> str:
    issued_at = str(int(time()))
    nonce = secrets.token_urlsafe(12)
    payload = f"{username}:{issued_at}:{nonce}"
    sig = hmac.new(_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _read_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        username, issued_at_str, nonce, sig = raw.split(":", 3)
        payload = f"{username}:{issued_at_str}:{nonce}"
        expected = hmac.new(_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        issued_at = int(issued_at_str)
        if int(time()) - issued_at > _SESSION_MAX_AGE:
            return None
        return username
    except Exception:
        return None


def _login_required_enabled() -> bool:
    try:
        return bool(get_settings().get("login_required", True))
    except Exception:
        return True


def _authenticated_user(token: str | None) -> str | None:
    user = _read_session(token)
    if user:
        return user
    if not _login_required_enabled():
        return "anonymous"
    return None


def _current_user(request: Request) -> str | None:
    return _authenticated_user(request.cookies.get(_SESSION_COOKIE))


def require_user(request: Request) -> str:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_session(request: Request) -> str:
    user = _read_session(request.cookies.get(_SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
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
        "filesize_bytes": job["filesize_bytes"],
        "codec": job["codec"],
        "bitrate_kbps": job["bitrate_kbps"],
    }

connections: set[WebSocket] = set()
event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

_HOUSEKEEPING_INTERVAL: int = 3600  # Every hour


async def _housekeeping_daemon() -> None:
    log = logging.getLogger("tubeyou.housekeeping")
    while True:
        await asyncio.sleep(_HOUSEKEEPING_INTERVAL)
        try:
            # Get retention days from settings (default 7)
            settings = get_settings()
            keep_days = settings.get("retention_days", 7)
            
            # Purge old jobs and get their IDs
            deleted_ids = purge_old_jobs(keep_days)
            
            if deleted_ids:
                # Delete job directories
                for job_id in deleted_ids:
                    job_dir = DATA_DIR / job_id
                    if job_dir.exists() and job_dir.is_dir():
                        try:
                            await asyncio.to_thread(shutil.rmtree, job_dir)
                            log.debug("Deleted job directory: %s", job_dir)
                        except Exception as e:
                            log.warning("Failed to delete directory %s: %s", job_dir, e)
                
                log.info("Housekeeping: %d job(s) older than %d days removed.", len(deleted_ids), keep_days)
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
        print(f"\n\033[91mFATAL: {msg}\033[0m", file=sys.stderr)
        print("\nInstall them with:", file=sys.stderr)
        print("  sudo apt-get install ffmpeg", file=sys.stderr)
        print("  pip install yt-dlp", file=sys.stderr)
        print("  # or: sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && sudo chmod +x /usr/local/bin/yt-dlp\n", file=sys.stderr)
        raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_dependencies()
    init_db()

    loop = asyncio.get_running_loop()

    def _thread_status_callback(payload: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, payload)

    set_status_callback(_thread_status_callback)
    start_workers(2)

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

        stop_workers(timeout=2.0)
        close_db()


app.router.lifespan_context = lifespan


async def _event_broadcaster():
    while True:
        payload = await event_queue.get()
        if not connections:
            continue

        sockets = list(connections)
        for ws in sockets:
            asyncio.create_task(_send_safe(ws, payload))


async def _send_safe(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(ws.send_json(payload), timeout=5.0)
    except Exception:
        connections.discard(ws)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    jobs = paginate_jobs(limit=50, offset=0)
    stats = get_stats()
    return templates.TemplateResponse(request=request, name="index.html", context={"jobs": jobs, "stats": stats})


@app.get("/api/jobs")
def api_jobs(user: str = Depends(require_user), offset: int = 0, limit: int = 50):
    _ = user

    safe_offset = max(0, offset)
    safe_limit = min(max(1, limit), 100)

    jobs = paginate_jobs(limit=safe_limit, offset=safe_offset)
    return [_job_to_dict(job) for job in jobs]


@app.get("/api/info")
@limiter.limit("10/minute")
async def api_info(request: Request, url: str, user: str = Depends(require_user)):
    """Extract video metadata using yt-dlp."""
    _ = (request, user)
    is_valid, error_msg = _validate_youtube_url(url)
    if not is_valid:
        return JSONResponse(status_code=400, content={"detail": error_msg})

    def _empty_info_payload() -> dict[str, object]:
        return {
            "title": None,
            "channel": None,
            "uploader": None,
            "duration": None,
            "view_count": None,
            "formats": [],
            "unavailable": True,
        }

    try:
        info: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                info = await asyncio.wait_for(asyncio.to_thread(_load_video_info, url.strip()), timeout=30.0)
                if info:
                    break
            except asyncio.TimeoutError:
                if attempt == 1:
                    raise
            except Exception:
                if attempt == 1:
                    raise
            await asyncio.sleep(0.75)

        if not info:
            return _empty_info_payload()

        # Extract format qualitites
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
        return _empty_info_payload()
    except Exception:
        return _empty_info_payload()


@app.get("/api/settings")
def api_get_settings(user: str = Depends(require_session)):
    """Get all settings."""
    _ = user
    settings = get_settings()
    return settings


@app.post("/api/settings")
async def api_set_settings(request: Request, user: str = Depends(require_session)):
    """Update settings (admin only - user is already admin via login)."""
    _ = user
    
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
    
    # Validate inputs
    if "retention_days" in payload:
        try:
            retention_days = int(payload["retention_days"])
            if retention_days < 1 or retention_days > 365:
                raise ValueError("retention_days must be between 1 and 365")
        except (ValueError, TypeError) as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
    
    if "admin_password" in payload and payload["admin_password"]:
        return JSONResponse(status_code=400, content={"detail": "Admin password cannot be changed via settings. Update TUBEYOU_ADMIN_PASSWORD environment variable and restart the app."})
    
    # Update settings
    try:
        settings_to_update = {}
        if "retention_days" in payload:
            settings_to_update["retention_days"] = payload["retention_days"]
        if "login_required" in payload:
            settings_to_update["login_required"] = payload["login_required"]
        if "lalalaai_api_key" in payload:
            lalalaai_api_key = str(payload["lalalaai_api_key"]).strip()
            if lalalaai_api_key:
                settings_to_update["lalalaai_api_key"] = lalalaai_api_key
        
        if settings_to_update:
            set_settings(settings_to_update)
        
        return {"ok": True, "message": "Settings updated"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Settings page."""
    if not _read_session(request.cookies.get(_SESSION_COOKIE)):
        return RedirectResponse(url="/login", status_code=303)
    
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": settings,
            "csrf_token": getattr(request.state, "csrf_token", ""),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": getattr(request.state, "csrf_token", "")},
    )


@app.post("/login")
async def login(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if not _verify_login(username, password):
        return JSONResponse(status_code=401, content={"ok": False, "detail": "Invalid username or password"})

    resp = JSONResponse(content={"ok": True, "redirect": "/"})
    resp.set_cookie(
        key=_SESSION_COOKIE,
        value=_sign_session(username),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=_SESSION_MAX_AGE,
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(
        _SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/submit")
@limiter.limit("5/minute")
async def api_submit(
    request: Request,
    url: str = Form(...),
    type: str = Form(...),
    quality: str = Form(...),
    user: str = Depends(require_user),
):
    _ = (request, user)
    
    # Validate YouTube URL
    is_valid, error_msg = _validate_youtube_url(url)
    if not is_valid:
        return JSONResponse(status_code=400, content={"detail": error_msg})

    meta = await asyncio.to_thread(_extract_video_meta, url.strip())
    
    job_id = str(uuid.uuid4())
    insert_job(
        job_id,
        url.strip(),
        type,
        quality,
        "queued",
        video_title=meta["video_title"] if isinstance(meta, dict) else None,
        video_meta_hover=meta["video_meta_hover"] if isinstance(meta, dict) else None,
    )
    await asyncio.to_thread(job_queue.put, (job_id, url.strip(), type, quality))
    job = get_job(job_id)
    return _job_to_dict(job)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    if not _current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    job = get_job(job_id)
    if not job:
        return templates.TemplateResponse(
            request=request, name="job.html", context={"job": None, "job_id": job_id}
        )

    return templates.TemplateResponse(request=request, name="job.html", context={"job": job})


@app.get("/download/{job_id}")
def download(request: Request, job_id: str):
    if not _current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    job = get_job(job_id)
    if not job or job["status"] != "done":
        return {"error": "not ready"}

    raw_filename = job["filename"]
    if not raw_filename:
        return JSONResponse(status_code=404, content={"error": "not ready"})

    file_path = Path(str(raw_filename)).resolve()
    try:
        file_path.relative_to(DATA_DIR)
    except ValueError:
        return JSONResponse(status_code=403, content={"error": "forbidden"})

    if not file_path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})

    return FileResponse(path=file_path, filename=file_path.name)


@app.get("/favicon.ico")
def favicon():
    """Serve favicon from static/img if exists, else 204."""
    ico_path = BASE_DIR / "static" / "img" / "favicon.ico"
    if ico_path.is_file():
        return FileResponse(path=ico_path, media_type="image/x-icon")
    # No favicon - return 204 No Content to silence browser errors
    return JSONResponse(status_code=204, content=None)


@app.get("/thumbnail/{job_id}")
def thumbnail(request: Request, job_id: str):
    """Serve cached thumbnail for a job."""
    if not _current_user(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    
    # Validate job_id is a valid UUID format
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid job_id"})
    
    thumb_path = DATA_DIR / job_id / "thumbnail.jpg"
    
    if not thumb_path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    
    return FileResponse(path=thumb_path, media_type="image/jpeg")


@app.websocket("/ws")
async def ws_status(websocket: WebSocket):
    if not _authenticated_user(websocket.cookies.get(_SESSION_COOKIE)):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.discard(websocket)
    except Exception:
        connections.discard(websocket)
