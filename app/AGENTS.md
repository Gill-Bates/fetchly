<!-- Parent: ../AGENTS.md -->

# app/ — FastAPI Backend

**Purpose:** Core FastAPI application, routing, worker coordination, database access, and business logic for media download, analysis, and processing.

The app module is the heart of fetchly: it handles HTTP requests, runs an in-process job queue, coordinates audio analysis, stores state in SQLite, and orchestrates interactions between the web UI and yt-dlp/FFmpeg/Lalal.ai.

## Key Files

| File      | Purpose                                                                                                                                              |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `main.py` | FastAPI app initialization, middleware stack, lifespan hooks, static/template mounting, router registration, auth-gated `/docs` and `/openapi.json`  |
| `db.py`   | Raw `sqlite3` access: schema creation, connection configuration, job/settings/share-link queries (including `list_active_jobs()`), status validation |

## Subdirectories

| Directory             | Purpose                                                                                                                                             |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `routes/`             | HTTP endpoint handlers: `api.py`, `auth.py`, `media.py`, `trim.py`, `cookies.py`, `lalal.py`, `share.py`, `events.py` (SSE)                         |
| `workers/`            | `worker.py` (download/transcode job processor) and `analysis_worker.py` (audio analysis submission)                                                 |
| `audio/`              | BPM and audio analysis: `analysis.py`, `cache.py`, `hashing.py`, `bpm.py`, `beat_this.py`, `cluster.py`, `naming.py`, `normalization.py`, `tags.py` |
| `integrations/lalal/` | Lalal.ai: `client.py` (API client) and `policy.py` (duration, quota and size guards)                                                                |
| `auth/`               | `session.py` (HMAC session tokens), `credentials.py` (admin account), `rate_limit.py` (trusted proxies, client IP, sliding-window limiter)               |
| `runtime/`            | `governor.py` (resource governor) and `gunicorn_logging.py` (`FetchlyGunicornLogger`)                                                               |
| `utils/`              | Utilities: version, changelog, host stats, watermark, cookies, platform quirks, template filters, housekeeping, public URL                          |
| `static/`             | Web assets: ES modules, CSS, images, self-hosted fonts, vendored Bootstrap 5 and WaveSurfer.js                                                      |
| `templates/`          | Jinja2 templates: `base.html`, `index.html`, `login.html`, `settings.html`, `job.html`, `share_error.html`, `macros/`, `_`-prefixed partials        |

## For AI Agents

### Working on Backend Features
- **Adding new API routes:** add the handler to the right file in `routes/`, export the router in `routes/__init__.py`, then call `app.include_router(...)` in `main.py` — `routes/__init__.py` only re-exports, it does not register anything
- **Job processing:** modify `workers/worker.py`; test status transitions, error handling, cancellation
- **Audio analysis:** `audio/` and `workers/analysis_worker.py`. Both detectors produce tempo: `audio/bpm.py` runs essentia's `RhythmExtractor2013` first, then beat-this, and fuses the two by confidence. The only results are `bpm` and `bpm_confidence` — there is no separate "audio features" stage. MP3 ID3 tags (cover, Fetchly comment, `TBPM`) come from `audio/tags.py`: written by `routes/media.py` when the MP3 is made, and `TBPM` is added in place later once the analysis result exists; the audio hash covers the stored source, not the MP3.
- **Database schema:** edit the `CREATE TABLE` statements in `db.py`'s `init_db()`. There are no migrations and no schema version: a database from an older release is not upgraded. `init_db()` checks existing tables against the `CREATE` statements (columns via `PRAGMA table_info`, extras tolerated) and raises `DatabaseSchemaError` with removal instructions (also for a file that is not a SQLite database or is corrupt; locked/busy errors pass through); `lifespan` logs it as `FATAL` and aborts startup. There is no `migration_version` table and no `_migrate_*` convention.
- **Authentication:** `auth/session.py` for tokens, `auth/credentials.py` for the admin account, `auth/rate_limit.py` for login limits; contract tests in `tests/test_auth_flow.py` and `tests/test_auth_credentials.py`
- **External APIs:** Lalal.ai in `integrations/lalal/client.py` / `policy.py`; platform quirks in `utils/platform.py` and `utils/youtube.py`

### Working on Frontend Integration
- **Rendering job states:** the valid status set lives in `db.py`; update templates and `static/js/` if you add one
- **Reload recovery:** `GET /` also passes `active_jobs` (`db.list_active_jobs()`, ignores `include_in_history`) as `activeJobsBootstrapData`; `static/js/current-job.js` restores them, and `job_to_dict()` adds the worker's live `progress`/`eta_seconds`. Covered by `tests/test_active_jobs_reload.py`.
- **SSE events:** `routes/events.py` streams `text/event-stream`; `static/js/events.js` consumes it via `EventSource`. This is not a WebSocket.
- **Settings persistence:** the `settings` table, read and written through `/api/settings`
- **Watermark logo:** uploaded via `POST /api/settings/watermark-logo`, served from `GET /api/settings/watermark-logo/image`, rendered by `utils/watermark_logo.py`

### Testing
- **Unit tests:** `tests/test_*.py` (57 files) — pytest is the runner, but tests are unittest-style classes
- **Shared setup:** subclass `IsolatedDbTestCase` or `WebAppTestCase` from `tests/_support.py`. `tests/conftest.py` only imports `_support` for its `FETCHLY_SECRET_KEY` side effect; it defines no fixtures.
- **API contract tests:** `tests/test_output_modes.py`
- **Worker hardening:** `tests/test_worker_hardening.py`
- **Database:** `tests/test_db_job_statuses.py`
- **Run tests:** `pytest`, or `pytest tests/test_worker_hardening.py -v`; `--cov` for coverage

### Common Patterns
- **Job statuses:** `queued`, `downloading`, `processing`, `transcoding`, `analysis`, `done`, `analysis_done`, `error`, `cancelled`. A `frozenset[str]` in `db.py`, validated on write — not an enum. Terminal set: `done`, `analysis_done`, `error`, `cancelled`; `ACTIVE_JOB_STATUSES` is every other status.
- **Worker pick-up:** `workers/worker.py` consumes an in-process `queue.Queue` sized by `WORKER_QUEUE_MAXSIZE`; a full queue rejects submissions with `503`. It does not poll the database on a timer.
- **Analysis flow:** after download the job moves to `analysis`, the analysis worker runs, results are written, and the status becomes `analysis_done`
- **Error handling:** worker exceptions are caught and logged, and the job is set to `error` with a message; the user can retry
- **Rate limiting:** in-house sliding-window limiter, `dependencies=[Depends(rate_limit("N/minute"))]` on sensitive routes; client IP resolved through the trusted-proxy logic in `auth/rate_limit.py`
- **CSRF:** `CSRFMiddleware` from `/middleware`, applied to the `/login`, `/logout` and `/api` prefixes

## Dependencies

### Internal
- `../middleware/csrf.py` — CSRF token generation/validation
- `./auth/rate_limit.py` — trusted-proxy and client-IP handling
- Cross-module: `workers/worker.py` imports `db`, `runtime.governor`, `workers.analysis_worker` and several `utils` modules, but **not** `routes`. Most route modules import `db`; `cookies.py` and `events.py` do not.

### External
- **Web:** FastAPI, Starlette, Uvicorn, uvicorn-worker, Gunicorn
- **Media:** yt-dlp (installed unpinned, outside `pyproject.toml`), yt-dlp-ejs, FFmpeg (subprocess)
- **Audio:** essentia (features; compiled from source in the image), beat-this (BPM, pulls torch), soundfile (beat-this's audio decoder since TorchAudio 2.9 moved decoding to the optional TorchCodec backend), numpy
- **Database:** SQLite via the stdlib `sqlite3` module
- **Security:** nh3 (HTML sanitizing), stdlib `hmac`/`secrets` for tokens
- **HTTP:** httpx (outbound calls to Lalal.ai and GitHub)
- **Templating:** Jinja2, markupsafe, markdown

## Manual

### Database Schema
Defined in `db.py`'s `init_db()`. Four tables:

- **jobs** — the job records the worker and UI operate on; status is constrained to the valid set
- **audio_analysis_cache** — analysis results keyed by audio content hash
- **share_links** — token, target job, usage counters and expiry
- **settings** — key/value store holding all runtime configuration, including the single `admin_username`

There is no `users` table and no `sessions` table. fetchly is single-admin: the account lives in `settings`, and sessions are stateless HMAC-signed tokens rather than database rows.

To add a field: extend the `CREATE TABLE` statement and the affected queries; existing databases are not altered.

### Worker Troubleshooting
If jobs stay in `queued`:
1. Confirm `WORKERS` is `1`. A second Gunicorn worker gets its own queue and its own governor, and neither can see the other's jobs.
2. Check the governor: `runtime/governor.py` sheds load when memory headroom drops below `MEMORY_THRESHOLD_MB` or the semaphores are saturated
3. Inspect the database: `sqlite3 "$DATA_DIR/jobs.db" "SELECT id, status FROM jobs WHERE status='queued' LIMIT 5"`
4. Check the logs on stdout/stderr — there is no log directory

### Adding a New Platform
1. Add the quirks to `utils/platform.py` (format filters, cookie requirements)
2. Add YouTube-specific handling to `utils/youtube.py` if relevant
3. Update the preview path in `routes/media.py` if the platform needs special handling
4. Extend `tests/test_output_modes.py`
5. Update the docs under `docs/features/`

### Logging
- **Application:** stdout/stderr. In the container, Gunicorn uses `FetchlyGunicornLogger` from `runtime/gunicorn_logging.py`.
- **Jobs:** failures are stored on the job record so the UI can show them
- There is no `/logs/` directory and no `analysis.log`

---

**Last Updated:** 2026-10-26  
**Python Version:** 3.13+  
**Persistence:** stdlib `sqlite3`, synchronous, WAL  
**Key Dependencies:** FastAPI, yt-dlp, essentia, beat-this
