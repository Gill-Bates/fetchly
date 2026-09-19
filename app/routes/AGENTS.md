<!-- Parent: ../AGENTS.md -->

# app/routes/ — HTTP Endpoint Handlers

**Purpose:** FastAPI route handlers implementing the JSON API, the server-rendered HTML pages, media delivery, and the SSE event stream.

Route handlers validate requests, call business logic (database access, worker submission, external APIs), and return responses (JSON, `TemplateResponse`, `FileResponse`, or an SSE stream).

## Key Files

| File | Purpose |
| --- | --- |
| `api.py` | Core API: job submission and listing, job cancel/retry, bulk removal, settings read/write, stats, BPM clusters, watermark logo upload/serve/delete, thumbnails, version info and update check. There is no single-job delete route — `POST /api/jobs/remove-all` is the only removal endpoint. |
| `auth.py` | Login page and POST, logout. The Lalal.ai activation key lives in `lalal.py`, not here. |
| `media.py` | Media delivery: job page, download, audio source, thumbnail, favicon. `build_job_file_response()` here is what `share.py` reuses for public share downloads. |
| `trim.py` | Audio trimming: clip submission, trim deletion and trimmed-file download |
| `cookies.py` | Per-platform cookie import (including paste), status and deletion |
| `lalal.py` | Lalal.ai stem separation: activation-key storage, auth status, split submission and result retrieval |
| `share.py` | Share links: creation and public `GET /share/{token}` access |
| `events.py` | **Server-Sent Events**: `GET /events` (all jobs) and `GET /api/jobs/{job_id}/events` (one job), both `text/event-stream` |
| `__init__.py` | Re-exports the eight routers (`api_router`, `auth_router`, …). It does **not** register them — `app/main.py` calls `include_router()` for each. |

## For AI Agents

### Adding New Endpoints
- **Location:** add the handler to the matching module, or create a new one
- **Registration:** export the router from `__init__.py`, then add `app.include_router(...)` in `app/main.py`
- **Authentication:** JSON endpoints depend on `require_user`; HTML pages call `require_html_auth(request)` and return its redirect when set. There is no `@requires_auth` decorator.
- **Rate limiting:** apply `@limiter.limit()` from `app/common/rate_limit.py` to sensitive endpoints
- **CSRF:** anything under `/api`, `/login` or `/logout` is covered automatically by `CSRFMiddleware`
- **Validation:** Pydantic models or explicit parsing; validate untrusted URLs before handing them to yt-dlp
- **Errors:** raise `HTTPException(status_code, detail)`; the detail string reaches the UI
- **Testing:** add a test class to `tests/` subclassing `WebAppTestCase` from `tests/_support.py`

### Endpoint Map
Real paths, as registered:

- **Jobs:** `POST /api/submit`, `GET /api/jobs`, `GET /api/jobs/{job_id}`, `POST /api/jobs/{job_id}/cancel`, `POST /api/jobs/{job_id}/retry`, `POST /api/jobs/remove-all`
- **Events (SSE):** `GET /events`, `GET /api/jobs/{job_id}/events`
- **Settings:** `GET /api/settings`, `POST /api/settings`, `GET|POST|DELETE /api/settings/watermark-logo`, `GET /api/settings/watermark-logo/image`
- **Stats & system:** `GET /api/stats`, `POST /api/stats/reset`, `GET /api/stats/bpm-clusters`, `GET /api/system/host`, `GET /api/info`, `GET /api/updates`
- **Media:** `GET /download/{job_id}`, `GET /audio-source/{job_id}`, `GET /thumbnail/{job_id}`, `GET /api/thumbnail/resolve`, `GET /api/thumbnail-proxy`, `GET /api/thumbnail-cache/{cache_key}`
- **Trim** (router prefix `/api/trim`): `POST /api/trim/{job_id}`, `DELETE /api/trim/{job_id}`, `GET /api/trim/{job_id}/{trim_id}/download`
- **Cookies** (router prefix `/api/cookies`): `GET /api/cookies`, `POST /api/cookies/{platform}`, `POST /api/cookies/{platform}/paste`, `DELETE /api/cookies/{platform}`
- **Lalal.ai** (router prefix `/api/lalal`): `GET /api/lalal/status`, `POST /api/lalal/auth/activation-key`, `POST /api/lalal/auth/logout`, `POST /api/lalal/{job_id}`, `GET /api/lalal/download/{job_id}`
- **Auth:** `GET /login`, `POST /login`, `POST /logout`
- **Share:** `POST /api/share/{job_id}`, `GET /share/{token}`
- **Pages:** `GET /`, `GET /job/{job_id}`, `GET /settings`, `GET /favicon.ico`
- **Ops:** `GET /health`, plus the auth-gated `GET /docs`, `GET /redoc` and `GET /openapi.json` registered directly on the app in `main.py`

Note the app-level auth endpoints live at `/login` and `/logout`, not under `/api/`.
The `/auth/...` paths belong to `lalal.py` and therefore carry its `/api/lalal` prefix.

### Working with the Job Lifecycle
- **Submission:** insert the job, enqueue it; a full worker queue is rejected with `503`
- **Status:** the valid set is defined in `app/db.py` — `queued`, `downloading`, `processing`, `transcoding`, `analysis`, `done`, `analysis_done`, `error`, `cancelled`
- **Cancellation:** `update_job_if_status()` guards the transition so a finished job is not re-cancelled
- **Completion:** the worker writes the terminal status; handlers read it back

### Working with External Services
- **yt-dlp:** invoked as a subprocess; never pass an unvalidated URL straight through
- **Lalal.ai:** `app/lalal.py` with the guards in `app/lalal_policy.py`
- **Watermark:** `app/utils/watermark.py`; uploads validated by `app/utils/watermark_logo.py`

### Common Patterns
- **File downloads:** `FileResponse()` with the right media type and `Content-Disposition`
- **HTML:** `TemplateResponse`, always passing `request`
- **JSON:** return a dict; FastAPI serializes it. Responses are shaped per endpoint — there is no global `{data, status, message}` envelope.
- **Errors:** always give a usable `detail`
- **Async:** handlers are `async def`; keep blocking work off the event loop (ruff's `ASYNC` rules are enabled)

## Dependencies

### Internal
- `../db.py` — job, settings and share-link queries
- `../governor.py` — resource headroom checks
- `../session.py` — session validation (`require_user`, `require_html_auth`)
- `../worker.py`, `../analysis_worker.py` — job submission
- `../lalal.py`, `../lalal_policy.py`, `../bpm*.py` — business logic
- `../utils/` — watermark, cookies, version, changelog, public URL, duration
- `../common/rate_limit.py` — limiter and client-IP resolution

### External
- **FastAPI / Starlette:** routing, dependencies, `StreamingResponse` for SSE
- **Pydantic:** request models
- **httpx:** outbound HTTP
- **yt-dlp, FFmpeg:** subprocesses

## Manual

### Adding a New Route
1. Write the handler in the appropriate module
2. Decorate it with `@router.get(...)` / `@router.post(...)` and the real path
3. Add the auth dependency (`require_user`) if it is not public
4. Add `@limiter.limit()` if it is sensitive
5. Export the router in `__init__.py` and include it in `main.py` if the module is new
6. Add a test subclassing `WebAppTestCase`
7. Update `docs/api/endpoints.md` if the endpoint is user-facing

### Common Route Pattern
```python
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_job
from .auth import require_user

router = APIRouter()


@router.get("/api/jobs/{job_id}")
async def get_job_handler(job_id: str, _user: str = Depends(require_user)):
    # db.py is synchronous sqlite3: every call belongs in a thread, never
    # straight on the event loop.
    job = await asyncio.to_thread(get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"id": job["id"], "status": job["status"]}
```

`require_user` / `require_user_json` / `require_html_auth` are defined in
`app/routes/auth.py`; `app/session.py` holds the lower-level token functions.

`get_job()` returns a `sqlite3.Row` or `None` — index it like a mapping, not like an ORM object.

### Debugging Route Issues
- **404:** confirm the router is exported in `__init__.py` *and* included in `main.py`
- **401 / redirect:** check `require_user` vs `require_html_auth` — the first returns JSON, the second redirects
- **403:** CSRF. The path is under a protected prefix and the token is missing or mismatched.
- **429:** rate limit; the handler in `main.py` returns `{"detail": "Rate limit exceeded"}`
- **503 on submit:** the worker queue is full (`WORKER_QUEUE_MAXSIZE`)

### SSE Events (events.py)
- **Connection:** the client opens `EventSource("/events")`; per-job streams use `/api/jobs/{job_id}/events`
- **Transport:** `StreamingResponse` with `media_type="text/event-stream"`. `SSEStreamingResponse` treats graceful-shutdown cancellation as a normal disconnect.
- **Publishing:** other modules call `publish_payload()`; `broadcast_shutdown()` notifies clients on shutdown
- **Cleanup:** subscribers are unregistered on disconnect; empty per-job subscriber sets are pruned

---

**Last Updated:** 2026-09-19  
**Framework:** FastAPI (Starlette)  
**Live Updates:** Server-Sent Events, not WebSocket  
**Auth Dependencies:** `require_user` (JSON), `require_html_auth` (pages)
