# tubeyou

tubeyou is a FastAPI application for authenticated YouTube download jobs with a live dashboard, SQLite persistence, waveform-based audio trimming, and optional Lalal.ai stem separation.

The current codebase targets Linux and Python 3.13. It runs locally via `run.py` or in Docker via Gunicorn with Uvicorn workers.

## Current Feature Set

- Session-protected dashboard with CSRF protection and automatic session renewal
- Submit YouTube video or audio jobs with metadata preview before enqueue
- Live job updates over WebSocket, including status, codec, bitrate, BPM, file size, and hover metadata
- Per-job detail page and direct download endpoint
- Audio-specific flow with lossless source serving, browser waveform trim UI, trimmed WAV downloads, and optional Lalal.ai vocals/instrumental splits
- Settings UI for retention, session idle timeout, Lalal.ai auth, and admin password rotation
- Password changes invalidate all active sessions and redirect users back to login
- Hourly housekeeping that removes expired completed/error jobs and matching artifacts
- Reverse-proxy-aware client IP handling for Uvicorn headers and SlowAPI rate limiting
- Playwright-based UI lint tooling for dashboard layout and infinite-scroll regressions

## Runtime Model

- App entry point: `app/main.py`
- Local launcher: `run.py`
- Database: `data/jobs.db` (SQLite, WAL mode)
- Job artifacts: `data/<job_id>/...`
- Workers: background downloader/transcoder workers plus audio-analysis workers start during app lifespan
- Housekeeping: runs every hour and applies the retention policy from settings

For audio jobs, tubeyou may keep the source file in a lossless or near-lossless format internally and transcode to MP3 on demand for browser downloads. The first MP3 download is cached for later requests.

## Requirements

### Local Runtime

- Linux
- Python 3.13
- `ffmpeg` in `PATH`
- `yt-dlp` in `PATH`

### Optional Developer Tooling

- Node.js for `tools/ui-lint`
- Playwright Chromium for the UI audit runner

The Docker image already installs `ffmpeg`, `yt-dlp`, Python dependencies, Gunicorn, and the entrypoint bootstrap logic.

## Local Start

Create a virtual environment, install dependencies, and run the app:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install yt-dlp

export TUBEYOU_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export TUBEYOU_ADMIN_PASSWORD="change-me"

python run.py
```

Then open `http://127.0.0.1:8000` and sign in with:

- username: `admin` by default
- password: the value of `TUBEYOU_ADMIN_PASSWORD`

Development reload mode:

```bash
LOG_LEVEL=debug UVICORN_RELOAD=true python run.py
```

In debug mode only, the app falls back to `admin` as the password if `TUBEYOU_ADMIN_PASSWORD` is unset. For any real deployment, set it explicitly.

Manual Uvicorn start is possible, but you should keep proxy handling aligned with `run.py`:

```bash
export FORWARDED_ALLOW_IPS="127.0.0.1,::1"
uvicorn app.main:app \
	--host 0.0.0.0 \
	--port 8000 \
	--proxy-headers \
	--forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
```

## Docker

Build a local image:

```bash
docker build -f docker/Dockerfile -t tubeyou .
```

The Dockerfile packages the current local checkout. Make sure your workspace is clean and `BUILD_INFO` is up to date before creating a release image.

Run it directly:

```bash
docker run --rm \
	-p 8000:8000 \
	-e TUBEYOU_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
	-e TUBEYOU_ADMIN_PASSWORD="change-me" \
	-v "$PWD/data:/app/data" \
	tubeyou
```

### Important Note About `docker/docker-compose.yml`

The checked-in compose file is a deployment-specific example, not a generic one-click default. It currently assumes:

- image `docker.cirrio.de/tubeyou:latest`
- an external Docker network named `cloudnet`
- fixed IPv4 and IPv6 addresses
- a sample secret in the file itself

Review and change those values before using it as-is.

The container entrypoint:

- bootstraps `/app/data`
- starts as root only long enough to fix bind-mount permissions
- drops to `appuser` via `gosu`
- auto-derives Gunicorn worker count from cgroup CPU limits when `WORKERS=auto`

## Reverse Proxy Notes

`run.py` and the Docker entrypoint both honor `FORWARDED_ALLOW_IPS` so that Uvicorn and SlowAPI use the real client IP instead of the proxy hop.

If you run tubeyou behind Caddy, Nginx, Traefik, or another reverse proxy on a non-default network, set `FORWARDED_ALLOW_IPS` to the proxy IP or CIDR ranges that should be trusted.

The repository includes a `Caddyfile`, but it is also deployment-specific and currently targets `yt.cirrio.de`.

## Environment Variables

### Required

- `TUBEYOU_SECRET_KEY`: secret used for session and CSRF token generation
- `TUBEYOU_ADMIN_PASSWORD`: required outside debug mode

### Authentication and UI

- `TUBEYOU_ADMIN_USER`: admin login name, default `admin`
- `TZ`: timezone used for rendered timestamps, default system/UTC behavior

### Local Runtime

- `LOG_LEVEL`: log level, default `info`
- `UVICORN_RELOAD`: enables reload in `run.py` when set to `1`, `true`, `yes`, or `on`
- `HOST`: bind host for `run.py`, default `0.0.0.0`
- `PORT`: bind port for `run.py`, default `8000`
- `FORWARDED_ALLOW_IPS`: trusted proxy IPs/CIDRs for forwarded headers and rate limiting

### WebSocket Limits

- `TUBEYOU_WS_MAX_CONNECTIONS`: maximum concurrent WebSocket connections, default `100`
- `TUBEYOU_WS_MAX_CONNECTIONS_PER_IP`: per-IP WebSocket cap, default `5`

### Docker / Gunicorn Entrypoint

- `APP_USER`: runtime user inside the container, default `appuser`
- `DATA_DIR`: artifact directory inside the container, default `/app/data`
- `WORKERS`: Gunicorn worker count or `auto`, default `auto`
- `MAX_WORKERS`: upper clamp for auto-derived workers, default `8`
- `TIMEOUT`: Gunicorn worker timeout in seconds, default `60`
- `UVICORN_HOST`, `UVICORN_PORT`, `UVICORN_WORKERS`: alternate names understood by the entrypoint

### Lalal.ai Configuration

Lalal.ai credentials are not read from environment variables in the current codepath. Configure them from the Settings UI, where the app stores the email/auth token state in SQLite settings.

## Main Routes

### UI

- `GET /` - dashboard
- `GET /settings` - settings UI
- `GET /login` - login page
- `POST /login` - login JSON endpoint
- `POST /logout` - logout endpoint
- `GET /job/{job_id}` - job detail page

### Core API

- `GET /health`
- `GET /api/jobs`
- `GET /api/info`
- `GET /api/stats/bpm-clusters`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/submit`
- `POST /api/jobs/{job_id}/cancel`

### Media and Audio

- `GET /download/{job_id}`
- `GET /audio-source/{job_id}`
- `GET /thumbnail/{job_id}`
- `POST /api/trim/{job_id}`
- `DELETE /api/trim/{job_id}`
- `GET /api/trim/{job_id}/{trim_id}/download`

### Lalal.ai

- `GET /api/lalal/status`
- `POST /api/lalal/auth/request`
- `GET /api/lalal/auth/cooldown`
- `POST /api/lalal/auth/verify`
- `POST /api/lalal/auth/activation-key`
- `POST /api/lalal/auth/logout`
- `POST /api/lalal/{job_id}`
- `GET /api/lalal/download/{job_id}`

### WebSocket

- `GET /ws` - authenticated status stream used by the dashboard

All UI/API routes except `/health` and `/login` require a valid authenticated session. The WebSocket endpoint also requires the session cookie.

## Data Layout

The application initializes `data/jobs.db` automatically on startup.

Typical per-job artifacts live under `data/<job_id>/` and may include:

- downloaded media output
- source audio files such as `*.source.opus`
- cached MP3 files
- `thumbnail.jpg`
- trimmed WAV files such as `trim_<startMs>_<endMs>.wav`
- Lalal split outputs such as `trim_<id>_vocals.mp3` and `trim_<id>_instrumental.mp3`

Housekeeping removes completed or errored jobs older than the configured retention window and deletes matching artifact directories.

## Settings and Defaults

The SQLite settings table is initialized with these important defaults:

- `retention_days = 7`
- `session_idle_minutes = 60`
- Lalal.ai auth values empty/off until configured

The Settings API/UI currently manages:

- retention window
- session idle timeout
- admin password rotation
- Lalal.ai email/token state

Changing the admin password increments the stored session version, invalidates all active sessions, deletes the current session cookie, and redirects clients back to `/login`.

## Developer Utilities

### UI Lint

The repository includes a Playwright-based UI audit runner in `tools/ui-lint`.

Install and run it with:

```bash
npm --prefix tools/ui-lint install
npm --prefix tools/ui-lint run install:browsers
npm --prefix tools/ui-lint run audit
```

It is used to catch dashboard layout regressions, sticky-header issues, scroll-container breakage, and jobs infinite-scroll contract drift.
