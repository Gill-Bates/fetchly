#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""TubeYou FastAPI Application - Main entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .common.rate_limit import get_trusted_proxy_hosts, limiter
from .analysis_worker import (
    set_status_callback as set_analysis_status_callback,
    start_analysis_workers,
    stop_analysis_workers,
    submit_analysis,
    SubmitResult,
)
from .db import (
    close_db,
    get_settings,
    init_db,
    list_jobs_requiring_audio_analysis,
    purge_old_jobs,
    update_job,
)
from .governor import governor
from .routes import auth_router, api_router, lalal_router, media_router, trim_router, ws_router
from .routes.auth import init_auth
from .routes.api import init_api
from .routes.lalal import init_lalal
from .routes.media import init_media, resolve_job_path
from .routes.trim import init_trim
from .routes.websocket import broadcast_payload, close_all_connections, connections
from .session import SESSION_COOKIE, renew_session, set_session_cookie
from .utils.housekeeping import cleanup_expired_jobs
from .utils.template_filters import register_filters
from .utils.version import BUILD_INFO, VERSION
from .worker import set_status_callback, signal_shutdown, start_workers, stop_workers
from middleware.csrf import CSRFMiddleware

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration & Constants
# ============================================================================

type EventPayload = dict[str, Any]
type EventQueue = asyncio.Queue[EventPayload]

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = (BASE_DIR.parent / "data").resolve()

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY")
if not _SECRET_KEY:
    raise RuntimeError("TUBEYOU_SECRET_KEY environment variable is required")

_DEV_MODE = os.environ.get("LOG_LEVEL", "").lower() == "debug"
_DEFAULT_USER = os.environ.get("TUBEYOU_ADMIN_USER", "admin")
_DEFAULT_PASS = os.environ.get("TUBEYOU_ADMIN_PASSWORD")
if not _DEFAULT_PASS:
    if _DEV_MODE:
        _DEFAULT_PASS = "admin"
    else:
        raise RuntimeError("TUBEYOU_ADMIN_PASSWORD environment variable is required")

_CSRF_COOKIE = "tubeyou_csrf"
_HOUSEKEEPING_INTERVAL = 3600  # Every hour
_EVENT_QUEUE_MAXSIZE = 10_000
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

# ============================================================================
# Templates & Event Dispatch
# ============================================================================

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Register template filters and globals
register_filters(templates)
templates.env.globals["now"] = lambda: datetime.now(UTC)
templates.env.globals["VERSION"] = VERSION
templates.env.globals["BUILD_INFO"] = BUILD_INFO

# Auth routes only depend on templates and env-backed credentials, so initialize
# them eagerly as well as during lifespan. This keeps TestClient(app) usable for
# login/logout checks even when startup events have not run yet.
init_auth(templates, _SECRET_KEY, _DEFAULT_USER, _DEFAULT_PASS)


def _make_event_callbacks(
    loop: asyncio.AbstractEventLoop,
    queue: EventQueue,
) -> tuple[Callable[[EventPayload], None], Callable[[EventPayload], None], Callable[[], None]]:
    """Bind event dispatch to a concrete queue and return loop/thread-safe callbacks."""
    accepting_events = True

    def _enqueue_event(payload: EventPayload) -> None:
        nonlocal accepting_events
        if not accepting_events:
            logger.debug("Status event dropped because shutdown is in progress")
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("Status event dropped because the event queue is full")

    def _thread_status_callback(payload: EventPayload) -> None:
        if not accepting_events:
            return
        try:
            loop.call_soon_threadsafe(_enqueue_event, payload)
        except RuntimeError:
            logger.debug("Status event dropped because the event loop is closing")

    def _disable_events() -> None:
        nonlocal accepting_events
        accepting_events = False

    return _enqueue_event, _thread_status_callback, _disable_events


# ============================================================================
# Background Tasks
# ============================================================================


async def _event_broadcaster(queue: EventQueue) -> None:
    """Broadcast status events to all connected WebSocket clients."""
    try:
        while True:
            payload = await queue.get()
            if not connections:
                continue
            sockets = list(connections)
            try:
                await broadcast_payload(payload, sockets)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Broadcasting status update failed")
    except asyncio.CancelledError:
        logger.debug("Event broadcaster cancelled")


def _run_housekeeping_once() -> None:
    """Load retention settings and purge expired jobs in one worker-thread call."""
    settings = get_settings()
    keep_days = settings.get("retention_days", 7)
    cleanup_expired_jobs(keep_days, DATA_DIR, purge_old_jobs)


async def _housekeeping_daemon() -> None:
    """Periodically clean up expired jobs and associated files using retention settings."""
    log = logging.getLogger("tubeyou.housekeeping")
    try:
        while True:
            await asyncio.sleep(_HOUSEKEEPING_INTERVAL)
            try:
                await asyncio.to_thread(_run_housekeeping_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Housekeeping failed: %s", exc)
    except asyncio.CancelledError:
        log.debug("Housekeeping daemon cancelled")


def _check_dependencies() -> None:
    """Verify required external tools are available.

    Raises:
        RuntimeError: If yt-dlp or ffmpeg is missing from PATH.
    """
    missing = []
    for cmd in ("yt-dlp", "ffmpeg"):
        if shutil.which(cmd) is None:
            missing.append(cmd)
    
    if missing:
        msg = f"Missing required dependencies: {', '.join(missing)}"
        logger.critical("FATAL: %s", msg)
        logger.critical("Install ffmpeg with: sudo apt-get install ffmpeg")
        logger.critical("Install yt-dlp with: pip install yt-dlp")
        raise RuntimeError(msg)


async def _cancel_background_tasks(tasks: list[asyncio.Task[None]], *, timeout: float = 2.0) -> None:
    """Cancel long-lived background tasks and wait briefly for them to exit."""
    if not tasks:
        return

    for task in tasks:
        task.cancel()

    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks, return_exceptions=True)
    except TimeoutError:
        for task in tasks:
            if not task.done():
                logger.warning("Background task %s did not stop within %.1fs", task.get_name(), timeout)


# ============================================================================
# Application Lifecycle
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    _ = app
    _check_dependencies()
    await asyncio.to_thread(init_db)
    event_queue: EventQueue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()
    enqueue_event, thread_status_callback, disable_event_dispatch = _make_event_callbacks(loop, event_queue)
    
    # Configure Governor for resource detection (Docker cgroup-aware)
    await asyncio.to_thread(governor.configure)
    
    # Initialize route modules
    await asyncio.to_thread(init_auth, templates, _SECRET_KEY, _DEFAULT_USER, _DEFAULT_PASS)
    init_api(templates, limiter, _DEFAULT_USER)
    init_media(DATA_DIR, BASE_DIR, templates, limiter)
    init_lalal(DATA_DIR, enqueue_event)
    init_trim(DATA_DIR, resolve_job_path)

    set_status_callback(thread_status_callback)
    set_analysis_status_callback(thread_status_callback)
    start_workers()
    start_analysis_workers()

    # Re-queue pending analysis jobs (capped to avoid startup delay)
    _MAX_REPLAY = 100
    pending_analysis_jobs = await asyncio.to_thread(list_jobs_requiring_audio_analysis)
    for i, row in enumerate(pending_analysis_jobs[:_MAX_REPLAY]):
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
            await asyncio.to_thread(update_job, str(row["id"]), status="done", message=msg)
        # Yield control every 10 jobs to keep event loop responsive
        if i % 10 == 9:
            await asyncio.sleep(0)

    if len(pending_analysis_jobs) > _MAX_REPLAY:
        logger.info(
            "Deferred %d pending analysis jobs to housekeeping",
            len(pending_analysis_jobs) - _MAX_REPLAY,
        )

    # Start background tasks that run for the full application lifespan.
    background_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_event_broadcaster(event_queue), name="event_broadcaster"),
        asyncio.create_task(_housekeeping_daemon(), name="housekeeping_daemon"),
    ]

    try:
        yield
    finally:
        logger.info("Shutting down...")
        disable_event_dispatch()
        set_status_callback(None)
        set_analysis_status_callback(None)
        signal_shutdown()

        await _cancel_background_tasks(background_tasks, timeout=2.0)

        await close_all_connections()

        # Run blocking stop functions in threads to avoid blocking event loop
        await asyncio.to_thread(stop_analysis_workers, 5.0)
        await asyncio.to_thread(stop_workers, 2.0)
        await asyncio.to_thread(close_db)
        logger.info("Shutdown complete")


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class SessionRenewalMiddleware(BaseHTTPMiddleware):
    """Renew session cookie on requests that carry a session cookie (sliding window).
    
    Skips renewal for static assets, auth endpoints, and other paths in _SKIP_RENEW_PATHS.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if response.status_code >= 400:
            return response

        request_path = request.url.path
        if any(request_path == prefix or request_path.startswith(f"{prefix}/") for prefix in _SKIP_RENEW_PATHS):
            return response

        old_token = request.cookies.get(SESSION_COOKIE)
        if old_token:
            # Offload to thread to avoid blocking event loop (token crypto can be slow)
            new_token = await asyncio.to_thread(renew_session, old_token)
            if new_token and new_token != old_token:
                set_session_cookie(response, new_token, request)
        return response


# Add middleware
app.add_middleware(SessionRenewalMiddleware)
app.add_middleware(CSRFMiddleware, csrf_cookie_name=_CSRF_COOKIE)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
# Proxy headers must wrap SlowAPI so request.client.host reflects the real
# client address when requests come through trusted reverse proxies like Caddy.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=get_trusted_proxy_hosts())


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit_exceeded(_request: Request, _exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


# Include route modules
app.include_router(auth_router)
app.include_router(api_router)
app.include_router(lalal_router)
app.include_router(media_router)
app.include_router(trim_router)
app.include_router(ws_router)
