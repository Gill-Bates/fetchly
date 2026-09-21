# fetchly Agent Guidance

**Purpose:** Root-level coordination guide for the fetchly self-hosted media downloader (FastAPI backend, server-rendered Jinja2 frontend). `pyproject.toml` carries the current version.

fetchly is a full-stack application that enables users to download, analyze, trim, and share media from YouTube, TikTok, Instagram, and Facebook. The backend is FastAPI (Python 3.13+) with a single-process job queue, yt-dlp media handling, BPM analysis, and Lalal.ai stem separation. The frontend is server-rendered Jinja2 plus hand-written ES modules — there is no build step and no frontend framework beyond vendored Bootstrap 5.

## Project Structure

| Directory | Purpose |
| --- | --- |
| `/app/` | FastAPI backend: routes, worker, audio analysis, database access, session handling |
| `/tests/` | Test suites: Python pytest (unittest-style classes) + Node.js `node:test` suites for UI contracts, accessibility, layout stability |
| `/tools/` | Development tooling: ESLint/Stylelint config, source-contract checker, Playwright-based UI audit, pyproject dependency exporter |
| `/middleware/` | Double-submit-cookie CSRF middleware |
| `/docker/` | Container runtime: Dockerfile, docker-compose, entrypoint script, APT repository preferences |
| `/docs/` | MkDocs documentation (config lives at `docs/mkdocs.yml`, not the repo root) |
| `/config/` | Reserved for static config templates; currently holds only its own AGENTS.md |
| `/.github/workflows/` | CI/CD: ruff, pytest, ESLint/Stylelint/contracts/`node --test`, UI audit, docker build, release automation |
| `/.github/agents/` | Standalone agent prompts (review suite + PythonDev implementation agent) |
| `/run.py` | Entry point for local development (uvicorn) |

## For AI Agents

### Working on Backend Features
- **Main entry:** `/app/main.py` (FastAPI app setup, middleware stack, lifespan hooks, static/template mounting, router registration)
- **Routes module:** `/app/routes/` — `api.py` (jobs, settings, stats, watermark logo), `auth.py` (login/logout/activation key), `media.py` (download, thumbnails, audio source), `trim.py` (clip editing), `lalal.py` (stem separation), `cookies.py` (platform auth), `share.py` (share links), `events.py` (**SSE**, not WebSocket)
- **Worker system:** `/app/worker.py` (in-process `queue.Queue` job processor) and `/app/analysis_worker.py` (BPM/audio analysis submission)
- **Database:** `/app/db.py` — raw `sqlite3` (stdlib, synchronous), hand-written SQL, `sqlite3.Row` results. No ORM, no SQLAlchemy, no Pydantic persistence models.
- **Business logic:** `/app/bpm.py` and the `bpm_*.py` modules, `/app/audio_analysis.py`, `/app/lalal.py`, `/app/lalal_policy.py`, `/app/governor.py` (resource limits), `/app/session.py` (HMAC-signed session tokens)
- **Utilities:** `/app/utils/` (version detection, changelog rendering, host stats, watermark rendering, cookie import/validation, template filters, housekeeping)
- **Dependencies:** FastAPI, Starlette, Uvicorn, Gunicorn, yt-dlp (installed unpinned, outside `pyproject.toml`), yt-dlp-ejs, essentia (audio features), beat-this (BPM, pulls torch), slowapi (rate limiting), pydantic (request validation)

### Working on Frontend Features
- **Static assets:** `/app/static/` — `js/` (ES modules, served as-is), `style.css` and `login.css` at the directory root, `img/`, `fonts/` (self-hosted), `vendor/` (Bootstrap 5 bundle, WaveSurfer.js)
- **Templates:** `/app/templates/` (Jinja2: `base.html`, `index.html`, `login.html`, `settings.html`, `job.html`, `share_error.html`, plus `macros/` and `_`-prefixed partials)
- **Framework:** none. Hand-written ES modules with `import`/`export`, no bundler, no `npm run build`.
- **Real-time:** Server-Sent Events. `/app/routes/events.py` returns `text/event-stream`; `/app/static/js/events.js` consumes it with `EventSource`.

### Testing
- **Python tests:** `/tests/test_*.py` — 40 files. pytest is the runner, but the tests are **unittest-style classes**; there are no pytest fixtures. Subclass `IsolatedDbTestCase` or `WebAppTestCase` from `/tests/_support.py`.
- **JavaScript tests:** `/tests/js/*.test.mjs` — 17 files using `node:test` + `node:assert/strict` with the shared fake DOM in `/tests/js/helpers/fake-dom.mjs`
- **UI audit:** `/tools/ui-lint/` — a separate Playwright suite run via `npm run ui-lint`, not part of `npm test`
- **CI gate:** ruff, pytest, ESLint, Stylelint, source contracts, and `node --test` must all pass

### Common Patterns
- **Job lifecycle:** API enqueues job → governor checks resource headroom → in-process worker picks it up → status written to database → clients receive SSE updates
- **Job statuses:** `queued`, `downloading`, `processing`, `transcoding`, `analysis`, `done`, `analysis_done`, `error`, `cancelled`. Defined as a `frozenset[str]` in `db.py` — there is no `JobStatus` enum, and no `active`/`completed`/`failed` status.
- **Audio analysis:** submitted to `analysis_worker`, which runs the detector cascade in a short-lived child process (essentia `RhythmExtractor2013`, then beat-this, fused by confidence). Results — `bpm` and `bpm_confidence`, including a "no usable tempo" outcome — are cached by audio hash in `audio_analysis_cache`. Files keep their stored names; the BPM tag only decorates the download name (`bpm_naming.py`).
- **Authentication:** optional (`enable_authentication` setting). Single admin credential in the `settings` table, HMAC-SHA256 session tokens, rate-limited login, invisible honeypot anti-bot token.
- **Configuration:** runtime settings live in the `settings` table; server-level config comes from environment variables (see `docs/configuration/environment.md`, the authoritative reference)
- **Media handling:** yt-dlp for format queries and download, FFmpeg for conversion/trimming/watermarking

## Dependencies

### Internal
- `/app/common/rate_limit.py` — trusted-proxy validation and client-IP resolution for rate limits
- `/middleware/csrf.py` — CSRF token generation and validation
- `app/routes/*` import `app/db.py`; `app/worker.py` imports `db`, `governor` and `analysis_worker` but **not** `app/routes/`

### External
- **Runtime:** FastAPI, Uvicorn, uvicorn-worker, Gunicorn, Jinja2, Pydantic, Slowapi, Starlette, yt-dlp, yt-dlp-ejs, essentia, beat-this, torch and torchaudio (CPU wheels), httpx, markdown, markupsafe, nh3, numpy, python-multipart
- **Development:** pytest, pytest-asyncio, pytest-cov, ruff (all pinned; runtime deps deliberately are not)
- **Documentation:** MkDocs, mkdocs-material, pymdown-extensions and the plugins listed under `[project.optional-dependencies] docs`
- **Frontend tooling:** ESLint, Stylelint, Playwright, @axe-core/playwright, pixelmatch, pngjs
- **Vendored in-tree:** Bootstrap 5, WaveSurfer.js (+ regions plugin)

## Manual

### Running Locally
```bash
python run.py       # Dev server on http://127.0.0.1:8000
# Creates/migrates the SQLite database on startup
```

### Testing
```bash
pytest                    # Run all Python tests (40 files)
npm test                  # node --test over tests/js/*.test.mjs (17 files)
npm run lint              # ESLint + Stylelint + source contracts
npm run ui-lint           # Playwright UI audit (needs: npm run ui-lint:install)
ruff check .              # Lint Python, as CI does
```

### Building & Deploying
- **Docker:** `docker build -f docker/Dockerfile -t fetchly:local .` — there is no Dockerfile at the repo root; the build context is the root
- **Release:** tag with `vX.Y.Z` format (must match `pyproject.toml`); `.github/workflows/docker-build.yml` builds multi-arch images and pushes to Docker Hub

### Key Configuration
Authoritative reference: `docs/configuration/environment.md`. Most-used variables:

- **FETCHLY_SECRET_KEY:** required; signs session cookies and the anti-bot token. The app refuses to start without it.
- **DATA_DIR:** `data/` locally, `/app/data` in the container. Holds `jobs.db`, downloads, cookie jars, caches.
- **WORKERS:** **must stay at `1`.** The job queue lives in-process; a second Gunicorn worker breaks job coordination.
- **FORWARDED_ALLOW_IPS:** trusted proxy IPs/CIDRs, default `127.0.0.1,::1`
- **FETCHLY_BEHIND_HTTPS:** set to `1` behind TLS so session cookies get `Secure`
- **Public share URLs:** derived from the request plus the `public_hostname` database setting. There is no `FETCHLY_PUBLIC_URL` variable.
- **Port:** defaults to 8000; run behind a reverse proxy (Caddy, Nginx) in production

### Release Checklist
- Update `pyproject.toml` version
- Update `CHANGELOG.md` with new features/fixes
- Push commit and tag with `git tag vX.Y.Z` (the version just written to `pyproject.toml`) and `git push --tags`
- `.github/workflows/docker-build.yml` builds and pushes images to Docker Hub

---

**Last Updated:** 2026-09-20  
**Version:** see `pyproject.toml` (`[project].version`)  
**Branch:** `feature/vX.Y.Z` during development, `main` for stable releases
