<!-- Parent: ../AGENTS.md -->

# app/ — FastAPI Backend

**Purpose:** Core FastAPI application, routing, worker coordination, database access, and business logic for media download, analysis, and processing.

The app module is the heart of fetchly: it handles HTTP requests, runs an in-process job queue, coordinates audio analysis, stores state in SQLite, and orchestrates interactions between the web UI and yt-dlp/FFmpeg/Lalal.ai.

## Key Files

| File | Purpose |
| --- | --- |
| `main.py` | FastAPI app initialization, middleware stack, lifespan hooks, static/template mounting, router registration, auth-gated `/docs` and `/openapi.json` |
| `worker.py` | Job processor: consumes an in-process `queue.Queue`, runs yt-dlp/FFmpeg as subprocesses, parses progress, manages the status lifecycle |
| `analysis_worker.py` | Submission layer for audio analysis; hands work off and writes results back to the database |
| `audio_analysis.py` | Audio analysis orchestration (feature extraction, result assembly) |
| `audio_cache.py` | Cache lookups and writes for analysis results |
| `audio_hash.py` | Content hashing used as the analysis cache key |
| `bpm.py` | BPM orchestration entry point |
| `bpm_beat_this.py` | beat-this model invocation for tempo detection |
| `bpm_cluster.py` | Clustering of detected BPM values |
| `bpm_naming.py` | Folds a detected tempo into the **download** filename (`Some Track_94bpm.source`). Stored filenames are never rewritten. |
| `bpm_normalization.py` | Octave/half-time normalization of raw BPM output |
| `db.py` | Raw `sqlite3` access: schema creation, connection configuration, job/settings/share-link queries, status validation |
| `session.py` | Session tokens signed with HMAC-SHA256, idle-timeout handling, login state |
| `governor.py` | Resource governor: cgroup/cpuset CPU detection, memory thresholds, semaphores, backpressure |
| `lalal.py` | Lalal.ai API integration: upload, stem request, polling, result retrieval |
| `lalal_policy.py` | Policy guards for Lalal.ai use (duration guard, quota and size caps) |
| `gunicorn_logging.py` | Custom Gunicorn logger class (`FetchlyGunicornLogger`) |

## Subdirectories

| Directory | Purpose |
| --- | --- |
| `routes/` | HTTP endpoint handlers: `api.py`, `auth.py`, `media.py`, `trim.py`, `cookies.py`, `lalal.py`, `share.py`, `events.py` (SSE) |
| `common/` | Shared logic: `rate_limit.py` (trusted-proxy validation, client-IP resolution, slowapi limiter) |
| `utils/` | Utilities: version, changelog, host stats, watermark, cookies, platform quirks, template filters, housekeeping, public URL |
| `static/` | Web assets: ES modules, CSS, images, self-hosted fonts, vendored Bootstrap 5 and WaveSurfer.js |
| `templates/` | Jinja2 templates: `base.html`, `index.html`, `login.html`, `settings.html`, `job.html`, `share_error.html`, `macros/`, `_`-prefixed partials |

## For AI Agents

### Working on Backend Features
- **Adding new API routes:** add the handler to the right file in `routes/`, export the router in `routes/__init__.py`, then call `app.include_router(...)` in `main.py` — `routes/__init__.py` only re-exports, it does not register anything
- **Job processing:** modify `worker.py`; test status transitions, error handling, cancellation
- **Audio analysis:** `bpm*.py`, `audio_analysis.py` and `analysis_worker.py`. Both detectors produce tempo: `bpm.py` runs essentia's `RhythmExtractor2013` first, then beat-this, and fuses the two by confidence. The only results are `bpm` and `bpm_confidence` — there is no separate "audio features" stage.
- **Database schema:** edit the `CREATE TABLE` statements in `db.py`'s `init_db()`. Schema changes are guarded by a stored schema version, and `db.py` refuses to open a database written by a newer fetchly. There is no `migration_version` table and no `_migrate_*` convention.
- **Authentication:** `session.py` for tokens, `utils/credentials.py` for the admin account, `common/rate_limit.py` for login limits; contract tests in `tests/test_auth_flow.py` and `tests/test_auth_credentials.py`
- **External APIs:** Lalal.ai in `lalal.py` / `lalal_policy.py`; platform quirks in `utils/platform.py` and `utils/youtube.py`

### Working on Frontend Integration
- **Rendering job states:** the valid status set lives in `db.py`; update templates and `static/js/` if you add one
- **SSE events:** `routes/events.py` streams `text/event-stream`; `static/js/events.js` consumes it via `EventSource`. This is not a WebSocket.
- **Settings persistence:** the `settings` table, read and written through `/api/settings`
- **Watermark logo:** uploaded via `POST /api/settings/watermark-logo`, served from `GET /api/settings/watermark-logo/image`, rendered by `utils/watermark_logo.py`

### Testing
- **Unit tests:** `tests/test_*.py` (39 files) — pytest is the runner, but tests are unittest-style classes
- **Shared setup:** subclass `IsolatedDbTestCase` or `WebAppTestCase` from `tests/_support.py`. `tests/conftest.py` only imports `_support` for its `FETCHLY_SECRET_KEY` side effect; it defines no fixtures.
- **API contract tests:** `tests/test_compatible_output.py`
- **Worker hardening:** `tests/test_worker_hardening.py`
- **Database:** `tests/test_db_job_statuses.py`
- **Run tests:** `pytest`, or `pytest tests/test_worker_hardening.py -v`; `--cov` for coverage

### Common Patterns
- **Job statuses:** `queued`, `downloading`, `processing`, `transcoding`, `analysis`, `done`, `analysis_done`, `error`, `cancelled`. A `frozenset[str]` in `db.py`, validated on write — not an enum. Terminal set: `done`, `analysis_done`, `error`, `cancelled`.
- **Worker pick-up:** `worker.py` consumes an in-process `queue.Queue` sized by `WORKER_QUEUE_MAXSIZE`; a full queue rejects submissions with `503`. It does not poll the database on a timer.
- **Analysis flow:** after download the job moves to `analysis`, the analysis worker runs, results are written, and the status becomes `analysis_done`
- **Error handling:** worker exceptions are caught and logged, and the job is set to `error` with a message; the user can retry
- **Rate limiting:** slowapi `@limiter.limit()` on sensitive routes; client IP resolved through the trusted-proxy logic in `common/rate_limit.py`
- **CSRF:** `CSRFMiddleware` from `/middleware`, applied to the `/login`, `/logout` and `/api` prefixes

## Dependencies

### Internal
- `../middleware/csrf.py` — CSRF token generation/validation
- `./common/rate_limit.py` — trusted-proxy and client-IP handling
- Cross-module: `worker.py` imports `db`, `governor`, `analysis_worker` and several `utils` modules, but **not** `routes`. All route modules import `db`.

### External
- **Web:** FastAPI, Starlette, Uvicorn, uvicorn-worker, Gunicorn
- **Media:** yt-dlp (installed unpinned, outside `pyproject.toml`), yt-dlp-ejs, FFmpeg (subprocess)
- **Audio:** essentia (features; compiled from source in the image), beat-this (BPM, pulls torch), numpy
- **Database:** SQLite via the stdlib `sqlite3` module
- **Security:** slowapi (rate limiting), nh3 (HTML sanitizing), stdlib `hmac`/`secrets` for tokens
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

To add a field: extend the `CREATE TABLE` statement and the affected queries, then bump the schema version guard.

### Worker Troubleshooting
If jobs stay in `queued`:
1. Confirm `WORKERS` is `1`. A second Gunicorn worker gets its own queue and its own governor, and neither can see the other's jobs.
2. Check the governor: `governor.py` sheds load when memory headroom drops below `MEMORY_THRESHOLD_MB` or the semaphores are saturated
3. Inspect the database: `sqlite3 "$DATA_DIR/jobs.db" "SELECT id, status FROM jobs WHERE status='queued' LIMIT 5"`
4. Check the logs on stdout/stderr — there is no log directory

### Adding a New Platform
1. Add the quirks to `utils/platform.py` (format filters, cookie requirements)
2. Add YouTube-specific handling to `utils/youtube.py` if relevant
3. Update the preview path in `routes/media.py` if the platform needs special handling
4. Extend `tests/test_compatible_output.py`
5. Update the docs under `docs/features/`

### Logging
- **Application:** stdout/stderr. In the container, Gunicorn uses `FetchlyGunicornLogger` from `gunicorn_logging.py`.
- **Jobs:** failures are stored on the job record so the UI can show them
- There is no `/logs/` directory and no `analysis.log`

---

**Last Updated:** 2026-09-19  
**Python Version:** 3.13+  
**Persistence:** stdlib `sqlite3`, synchronous, WAL  
**Key Dependencies:** FastAPI, yt-dlp, essentia, beat-this
