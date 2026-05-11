#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""TubeYou FastAPI Application - Main entry point."""

import asyncio
import logging
import os
import queue
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
from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
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
    cancel_interrupted_jobs,
    close_db,
    get_settings,
    init_db,
    list_queued_jobs,
    list_jobs_requiring_audio_analysis,
    purge_old_jobs,
    update_job,
)
from .governor import governor
from .routes import auth_router, api_router, events_router, lalal_router, media_router, trim_router
from .routes.auth import init_auth
from .routes.api import init_api
from .routes.lalal import init_lalal
from .routes.media import init_media, resolve_job_path
from .routes.trim import init_trim
from .routes.events import (
    init_sse_shutdown_event,
    publish_payload,
    signal_sse_shutdown,
)
from .session import SESSION_COOKIE, refresh_session_settings_cache, renew_session, set_session_cookie
from .utils.housekeeping import cleanup_expired_jobs
from .utils.template_filters import register_filters
from .utils.version import BUILD_INFO, VERSION
from .worker import get_job_queue, set_status_callback, signal_shutdown, start_workers, stop_workers
from middleware.csrf import CSRFMiddleware

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration & Constants
# ============================================================================

type EventPayload = dict[str, Any]
type EventQueue = asyncio.Queue[EventPayload]

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path(os.environ.get("TUBEYOU_DATA_DIR", str(BASE_DIR.parent / "data"))).resolve()

_SECRET_KEY = os.environ.get("TUBEYOU_SECRET_KEY")
if not _SECRET_KEY:
    raise RuntimeError("TUBEYOU_SECRET_KEY environment variable is required")

_DEFAULT_USER = os.environ.get("TUBEYOU_ADMIN_USER", "admin")
_DEFAULT_PASS = os.environ.get("TUBEYOU_ADMIN_PASSWORD")
if not _DEFAULT_PASS:
    raise RuntimeError("TUBEYOU_ADMIN_PASSWORD environment variable is required")

_CSRF_COOKIE = "tubeyou_csrf"
_HOUSEKEEPING_INTERVAL = 3600  # Every hour
_EVENT_QUEUE_MAXSIZE = 10_000
_SESSION_SETTINGS_REFRESH_INTERVAL = 60.0
_SKIP_RENEW_EXACT = frozenset({
    "/favicon.ico",
    "/health",
    "/login",
    "/logout",
})
_SKIP_RENEW_PREFIXES = (
    "/static",
    "/thumbnail",
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


def _should_skip_session_renewal(request_path: str) -> bool:
    if request_path in _SKIP_RENEW_EXACT:
        return True
    return any(request_path.startswith(prefix) for prefix in _SKIP_RENEW_PREFIXES)


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
    """Broadcast status events to all connected SSE clients."""
    try:
        while True:
            payload = await queue.get()
            try:
                publish_payload(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, MemoryError):
                    raise
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
                if isinstance(exc, MemoryError):
                    raise
                log.warning("Housekeeping failed: %s", exc)
    except asyncio.CancelledError:
        log.debug("Housekeeping daemon cancelled")


async def _requeue_pending_download_jobs() -> None:
    """Replay persisted queued jobs into the in-memory worker queue on startup."""
    pending_jobs = await asyncio.to_thread(list_queued_jobs)
    if not pending_jobs:
        return

    job_queue = get_job_queue()
    for index, row in enumerate(pending_jobs, start=1):
        job = (
            str(row["id"]),
            str(row["url"]),
            str(row["type"]),
            str(row["quality"]),
        )
        try:
            job_queue.put_nowait(job)
        except queue.Full:
            logger.warning("Job queue full during startup replay, dropping job %s", row["id"])
            break
        if index % 10 == 0:
            await asyncio.sleep(0)

    logger.info("Re-queued %d persisted download jobs during startup", len(pending_jobs))


async def _session_settings_refresh_daemon() -> None:
    """Refresh cached session settings outside request handling."""
    try:
        while True:
            await asyncio.sleep(_SESSION_SETTINGS_REFRESH_INTERVAL)
            try:
                await asyncio.to_thread(refresh_session_settings_cache)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, MemoryError):
                    raise
                logger.exception("Session settings refresh failed")
    except asyncio.CancelledError:
        logger.debug("Session settings refresh daemon cancelled")


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


def _log_background_task_completion(task: asyncio.Task[None]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return

    if exc is not None:
        logger.critical("Background task %s crashed", task.get_name(), exc_info=exc)


# ============================================================================
# Application Lifecycle
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    _ = app
    _check_dependencies()
    await asyncio.to_thread(DATA_DIR.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(init_db)
    init_sse_shutdown_event()
    await asyncio.to_thread(refresh_session_settings_cache)
    recovered_jobs = await asyncio.to_thread(cancel_interrupted_jobs)
    if recovered_jobs:
        logger.warning("Marked %d interrupted in-flight jobs as cancelled during startup", recovered_jobs)
    event_queue: EventQueue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()
    enqueue_event, thread_status_callback, disable_event_dispatch = _make_event_callbacks(loop, event_queue)
    
    # Configure Governor for resource detection (Docker cgroup-aware)
    await asyncio.to_thread(governor.configure)
    
    # Initialize route modules
    init_api(templates, _DEFAULT_USER)
    init_media(DATA_DIR, BASE_DIR, templates)
    init_lalal(DATA_DIR, enqueue_event)
    init_trim(DATA_DIR, resolve_job_path)

    set_status_callback(thread_status_callback)
    set_analysis_status_callback(thread_status_callback)
    start_workers()
    start_analysis_workers()
    await _requeue_pending_download_jobs()

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
        asyncio.create_task(_session_settings_refresh_daemon(), name="session_settings_refresh_daemon"),
    ]
    for task in background_tasks:
        task.add_done_callback(_log_background_task_completion)

    try:
        yield
    finally:
        logger.info("Shutting down...")
        signal_sse_shutdown()
        disable_event_dispatch()
        set_status_callback(None)
        set_analysis_status_callback(None)
        signal_shutdown()

        await _cancel_background_tasks(background_tasks, timeout=2.0)

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


class SessionRenewalMiddleware:
    """Renew session cookies without BaseHTTPMiddleware's streaming-response wrapper."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        request_path = request.url.path
        old_token = request.cookies.get(SESSION_COOKIE)
        renewed_token: str | None = None

        if old_token and not _should_skip_session_renewal(request_path):
            renewed_token = await asyncio.to_thread(renew_session, old_token)
            if renewed_token == old_token:
                renewed_token = None

        async def send_wrapper(message: Message) -> None:
            if renewed_token and message.get("type") == "http.response.start":
                status = int(message.get("status", 200))
                if status < 400:
                    headers = MutableHeaders(scope=message)
                    response = Response()
                    set_session_cookie(response, renewed_token, request)
                    for key, value in response.raw_headers:
                        if key == b"set-cookie":
                            headers.append("set-cookie", value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Add middleware
app.add_middleware(SessionRenewalMiddleware)
app.add_middleware(
    CSRFMiddleware,
    csrf_cookie_name=_CSRF_COOKIE,
    protected_paths=("/login", "/logout", "/api"),
)
app.state.limiter = limiter
# Decorator-based SlowAPI checks use request.client.host, so proxy headers must
# wrap the app before route handlers run when requests come through Caddy.
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
app.include_router(events_router)
