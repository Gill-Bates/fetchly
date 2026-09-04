#!/usr/bin/env python3
#
# app/main.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Fetchly FastAPI Application - Main entry point."""

import asyncio
import logging
import os
import shutil
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi.errors import RateLimitExceeded
from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from middleware.csrf import CSRFMiddleware

from .analysis_worker import (
    SubmitResult,
    start_analysis_workers,
    stop_analysis_workers,
    submit_analysis,
)
from .analysis_worker import (
    set_status_callback as set_analysis_status_callback,
)
from .common.rate_limit import get_trusted_proxy_hosts, limiter, validate_trusted_proxy_hosts
from .db import (
    cancel_interrupted_jobs,
    close_db,
    delete_share_links_for_jobs,
    get_settings,
    init_db,
    job_exists,
    list_expired_job_ids,
    list_jobs_requiring_audio_analysis,
    list_queued_jobs,
)
from .governor import GovernorConfig, governor
from .lalal_policy import LALAL_MAX_DURATION_MINUTES, LALAL_MAX_DURATION_SECONDS
from .routes import (
    api_router,
    auth_router,
    cookies_router,
    events_router,
    lalal_router,
    media_router,
    share_router,
    trim_router,
)
from .routes.api import init_api
from .routes.auth import init_auth, require_html_auth
from .routes.events import (
    init_sse_shutdown_event,
    publish_payload,
    signal_sse_shutdown,
)
from .routes.lalal import init_lalal
from .routes.media import init_media, resolve_job_path
from .routes.share import init_share
from .routes.trim import init_trim
from .session import SESSION_COOKIE, refresh_session_settings_cache, renew_session, set_session_cookie
from .utils.cookies import ensure_data_cookies_dir
from .utils.duration import round_seconds
from .utils.fs import get_data_dir
from .utils.housekeeping import cleanup_expired_jobs, cleanup_orphaned_directories, cleanup_thumbnail_cache
from .utils.template_filters import register_filters
from .utils.version import BUILD_INFO, VERSION
from .worker import set_status_callback, signal_shutdown, start_workers, stop_workers, submit_download

logger = logging.getLogger(__name__)

type EventPayload = dict[str, Any]
type EventQueue = asyncio.Queue[EventPayload]

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = get_data_dir()

_SECRET_KEY = os.environ.get("FETCHLY_SECRET_KEY")
if not _SECRET_KEY:
    raise RuntimeError("FETCHLY_SECRET_KEY environment variable is required")

# No admin credentials come from the environment. fetchly starts with
# authentication switched off and an empty account; the admin username and
# password are created in Settings -> Security and stored in the database.
_CSRF_COOKIE = "fetchly_csrf"
_HOUSEKEEPING_INTERVAL = 3600  # Every hour
_ANALYSIS_BACKLOG_POLL_INTERVAL = 5.0
# Only startup replays of a backlog larger than the queue leave jobs persisted
# but unqueued, so this poll can be lazier than the analysis one.
_DOWNLOAD_BACKLOG_POLL_INTERVAL = 15.0
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

templates = Jinja2Templates(directory=BASE_DIR / "templates")

register_filters(templates)
templates.env.globals["now"] = lambda: datetime.now(UTC)
templates.env.globals["VERSION"] = VERSION
templates.env.globals["BUILD_INFO"] = BUILD_INFO
templates.env.globals["CSRF_COOKIE_NAME"] = _CSRF_COOKIE
templates.env.globals["LALAL_MAX_DURATION_SECONDS"] = LALAL_MAX_DURATION_SECONDS
templates.env.globals["LALAL_MAX_DURATION_MINUTES"] = LALAL_MAX_DURATION_MINUTES

# Auth routes only depend on templates and the signing key, so initialize them
# eagerly as well as during lifespan. This keeps TestClient(app) usable for
# login/logout checks even when startup events have not run yet.
init_auth(templates, _SECRET_KEY)


def _should_skip_session_renewal(request_path: str) -> bool:
    if request_path in _SKIP_RENEW_EXACT:
        return True
    return any(request_path.startswith(prefix) for prefix in _SKIP_RENEW_PREFIXES)


def _governor_config_from_settings(settings: dict[str, Any]) -> GovernorConfig:
    """Build startup Governor config from persisted runtime settings plus env."""
    config = GovernorConfig.from_env()
    config.worker_count = int(settings.get("download_worker_count", 0) or 0)
    return config


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
    """One retention sweep: read settings, clean expired job files and orphans."""
    settings = get_settings()
    keep_days = settings.get("retention_days", 0)
    expired_ids = list_expired_job_ids(keep_days)
    cleanup_expired_jobs(keep_days, DATA_DIR, lambda _days: expired_ids)
    # Artifacts for these jobs are gone, so their share links can only 404 from
    # here on. Dropping them keeps the table from growing without bound.
    removed_links = delete_share_links_for_jobs(expired_ids)
    if removed_links:
        logger.info("Housekeeping: removed %d share link(s) for expired jobs", removed_links)
    cleanup_thumbnail_cache(DATA_DIR / "thumb-cache")
    # Retained DB rows protect their directories from this orphan sweep. Only
    # directories with no corresponding job record are removed here.
    cleanup_orphaned_directories(DATA_DIR, job_exists)


async def _housekeeping_daemon() -> None:
    """Run _run_housekeeping_once() on a fixed interval."""
    log = logging.getLogger("fetchly.housekeeping")
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


async def _fill_download_queue() -> None:
    """Feed persisted queued download jobs into free in-memory queue slots.

    ``submit_download`` skips ids that already hold a slot, so this is safe to
    run repeatedly: rows that did not fit stay ``queued`` and are picked up by
    the next pass instead of being dropped until the next restart.
    """
    pending_jobs = await asyncio.to_thread(list_queued_jobs)
    if not pending_jobs:
        return

    queued = 0
    for index, row in enumerate(pending_jobs):
        job = (
            str(row["id"]),
            str(row["url"]),
            str(row["type"]),
            str(row["quality"]),
        )
        if not submit_download(job):
            logger.info(
                "Download queue is full; %d job(s) remain persisted for retry",
                len(pending_jobs) - index,
            )
            break
        queued += 1
        if index % 10 == 9:
            await asyncio.sleep(0)

    if queued:
        logger.debug("Handed %d persisted download job(s) to the worker queue", queued)


async def _download_backlog_daemon() -> None:
    """Continuously feed persisted download jobs into available worker slots."""
    try:
        while True:
            try:
                await _fill_download_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Download backlog refill failed")
            await asyncio.sleep(_DOWNLOAD_BACKLOG_POLL_INTERVAL)
    except asyncio.CancelledError:
        logger.debug("Download backlog daemon cancelled")


async def _fill_analysis_queue() -> None:
    """Queue persisted analysis jobs until in-memory capacity is reached."""
    pending_analysis_jobs = await asyncio.to_thread(list_jobs_requiring_audio_analysis)
    for index, row in enumerate(pending_analysis_jobs):
        raw_filename = str(row["filename"] or "").strip()
        if not raw_filename:
            logger.warning("Skipping analysis replay for %s without an audio filename", row["id"])
            continue

        result = submit_analysis(
            str(row["id"]),
            Path(raw_filename),
            duration_seconds=round_seconds(row["duration_seconds"]),
            block=False,
        )
        if result is SubmitResult.QUEUE_FULL:
            logger.info(
                "Analysis queue is full; %d jobs remain persisted for retry",
                len(pending_analysis_jobs) - index,
            )
            return
        if result is SubmitResult.REJECTED_SHUTDOWN:
            return
        if index % 10 == 9:
            await asyncio.sleep(0)


async def _analysis_backlog_daemon() -> None:
    """Continuously feed persisted analysis jobs into available worker slots."""
    try:
        while True:
            try:
                await _fill_analysis_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Analysis backlog refill failed")
            await asyncio.sleep(_ANALYSIS_BACKLOG_POLL_INTERVAL)
    except asyncio.CancelledError:
        logger.debug("Analysis backlog daemon cancelled")


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
    """Raise RuntimeError if yt-dlp or ffmpeg is missing from PATH."""
    missing = [cmd for cmd in ("yt-dlp", "ffmpeg") if shutil.which(cmd) is None]

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


type _SignalHandler = Callable[[int, FrameType | None], Any] | int | None


def _install_shutdown_signal_hook() -> Callable[[], None]:
    """Close SSE streams as soon as a termination signal arrives.

    Uvicorn waits for open connections *before* emitting the lifespan shutdown
    event, so signalling from the lifespan teardown only fires after the
    graceful-shutdown timeout. Chaining onto the server's own signal handler
    ends the streams while it is still waiting. Returns a handler-restore
    callable.
    """
    loop = asyncio.get_running_loop()
    previous: dict[int, _SignalHandler] = {}

    def _restore() -> None:
        for sig, handler in previous.items():
            with suppress(ValueError):
                signal.signal(sig, handler)
        previous.clear()

    def _handle_shutdown_signal(sig: int, frame: FrameType | None) -> None:
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(signal_sse_shutdown)
        chained = previous.get(sig)
        if callable(chained):
            chained(sig, frame)
        elif chained is signal.SIG_DFL:
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, _handle_shutdown_signal)
    except ValueError:
        # Signal handlers can only be installed from the main thread (for
        # example TestClient runs the app in a worker thread).
        _restore()
        return lambda: None

    return _restore


def _log_background_task_completion(task: asyncio.Task[None]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return

    if exc is not None:
        logger.critical("Background task %s crashed", task.get_name(), exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = app
    _check_dependencies()
    await asyncio.to_thread(DATA_DIR.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(ensure_data_cookies_dir)
    await asyncio.to_thread(init_db)
    init_sse_shutdown_event()
    restore_shutdown_signals = _install_shutdown_signal_hook()
    await asyncio.to_thread(refresh_session_settings_cache)
    recovered_jobs = await asyncio.to_thread(cancel_interrupted_jobs)
    if recovered_jobs:
        logger.warning("Marked %d interrupted in-flight jobs as cancelled during startup", recovered_jobs)
    event_queue: EventQueue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()
    enqueue_event, thread_status_callback, disable_event_dispatch = _make_event_callbacks(loop, event_queue)

    # Download-worker changes apply after restart because the worker pool and
    # queue are both startup-only process-local structures.
    startup_settings = await asyncio.to_thread(get_settings)
    await asyncio.to_thread(governor.configure, _governor_config_from_settings(startup_settings))

    init_api(templates)
    init_media(DATA_DIR, BASE_DIR, templates)
    init_share(templates)
    init_lalal(DATA_DIR, enqueue_event)
    init_trim(DATA_DIR, resolve_job_path)

    set_status_callback(thread_status_callback)
    set_analysis_status_callback(thread_status_callback)
    start_workers()
    start_analysis_workers()
    await _fill_download_queue()

    await _fill_analysis_queue()

    background_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_event_broadcaster(event_queue), name="event_broadcaster"),
        asyncio.create_task(_housekeeping_daemon(), name="housekeeping_daemon"),
        asyncio.create_task(_analysis_backlog_daemon(), name="analysis_backlog_daemon"),
        asyncio.create_task(_download_backlog_daemon(), name="download_backlog_daemon"),
        asyncio.create_task(_session_settings_refresh_daemon(), name="session_settings_refresh_daemon"),
    ]
    for task in background_tasks:
        task.add_done_callback(_log_background_task_completion)

    try:
        yield
    finally:
        restore_shutdown_signals()
        logger.info("Shutting down...")
        signal_sse_shutdown()
        disable_event_dispatch()
        set_status_callback(None)
        set_analysis_status_callback(None)
        signal_shutdown()

        await _cancel_background_tasks(background_tasks, timeout=2.0)

        # Blocking stops run in threads so the event loop stays responsive.
        await asyncio.to_thread(stop_analysis_workers, 5.0)
        await asyncio.to_thread(stop_workers, 2.0)
        await asyncio.to_thread(close_db)
        logger.info("Shutdown complete")


# Swagger UI, ReDoc, and the raw schema are disabled at their default paths and
# re-registered below behind the same session check as every HTML page (see
# docs/api/overview.md: "these are not public endpoints"). Once authentication
# is enabled, /docs, /redoc, and /openapi.json redirect to /login exactly like
# / and /settings do; with authentication off they remain reachable, matching
# every other route in that mode.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/openapi.json", include_in_schema=False, response_model=None)
async def _openapi_schema(request: Request) -> Response | JSONResponse:
    redirect = require_html_auth(request)
    if redirect:
        return redirect
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
async def _swagger_ui(request: Request) -> Response:
    redirect = require_html_auth(request)
    if redirect:
        return redirect
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", include_in_schema=False)
async def _redoc_ui(request: Request) -> Response:
    redirect = require_html_auth(request)
    if redirect:
        return redirect
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


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
                    try:
                        response = Response()
                        set_session_cookie(response, renewed_token, request)
                    except ValueError:
                        # renewed_token was computed from a session that was
                        # live at request start, but the handler itself can
                        # invalidate every outstanding session (e.g. a
                        # password change bumping session_version - see
                        # api_set_settings in app/routes/api.py) before this
                        # response is sent. set_session_cookie correctly
                        # refuses to reissue a now-invalid token; skip the
                        # renewal instead of turning that into a 500, and
                        # leave the handler's own cookie handling (if any) as
                        # the source of truth.
                        logger.debug(
                            "Skipping session renewal: token invalidated during request handling",
                            exc_info=True,
                        )
                    else:
                        headers = MutableHeaders(scope=message)
                        for key, value in response.raw_headers:
                            if key == b"set-cookie":
                                headers.append("set-cookie", value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


# SHA-256 of the stylesheet WaveSurfer injects into its shadow root, so
# style-src can allow that one sheet without opening up 'unsafe-inline'.
# Refresh it (from the browser console's CSP error) whenever the vendored
# bundle or the trim view's height option changes.
_WAVESURFER_STYLE_HASH: Final = "sha256-7XP2opZSAzH42qQ4QpsmFbwnOyeFkvJlqZeHR2BRgEA="


class SecurityHeadersMiddleware:
    """Attach baseline security headers to all HTTP responses."""

    _CSP = "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        # No 'unsafe-inline': templates have no <style>/style= and progress
        # updates use element.style.* (CSSOM, not subject to style-src). The
        # hash covers the one <style> the app does not author - WaveSurfer's
        # shadow-root sheet, which positions the progress canvas over the
        # waveform and interpolates the configured height (trim.js). Drift in
        # either fails tests/test_csp_wavesurfer.py.
        f"style-src 'self' '{_WAVESURFER_STYLE_HASH}'",
        "img-src 'self' data: https://img.youtube.com https://i.ytimg.com",
        "font-src 'self'",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "worker-src blob:",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", self._CSP)
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                if scope.get("scheme") == "https":
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class OriginalClientMiddleware:
    """Preserve the socket peer before proxy headers rewrite ``scope['client']``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") in {"http", "websocket"}:
            scope["fetchly.original_client"] = scope.get("client")
        await self.app(scope, receive, send)


app.add_middleware(SessionRenewalMiddleware)
app.add_middleware(
    CSRFMiddleware,
    csrf_cookie_name=_CSRF_COOKIE,
    protected_paths=("/login", "/logout", "/api"),
)
app.add_middleware(SecurityHeadersMiddleware)
app.state.limiter = limiter
trusted_proxy_hosts = get_trusted_proxy_hosts()
trusted_proxy_hosts = validate_trusted_proxy_hosts(trusted_proxy_hosts)
# Decorator-based SlowAPI checks use request.client.host, so proxy headers must
# wrap the app before route handlers run when requests come through Caddy.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxy_hosts)
# This must be added after ProxyHeadersMiddleware: Starlette wraps the most
# recently added middleware outside the earlier ones, preserving the raw peer
# before ProxyHeadersMiddleware applies the validated X-Forwarded-* headers.
app.add_middleware(OriginalClientMiddleware)


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit_exceeded(_request: Request, _exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


app.include_router(auth_router)
app.include_router(api_router)
app.include_router(cookies_router)
app.include_router(lalal_router)
app.include_router(media_router)
app.include_router(share_router)
app.include_router(trim_router)
app.include_router(events_router)
