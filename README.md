# tubeyou

tubeyou is a FastAPI web app for downloading and converting YouTube videos.
It stores job history in SQLite, provides a live-updating dashboard, optional login protection, and a settings UI for retention, sessions, and Lalal.ai integration.

## Features
- YouTube URL validation and async job submission
- Download and conversion pipeline with MP3 output support
- Real-time status updates via WebSocket
- Job overview with title, status, codec, bitrate, size, and timestamps
- Optional login requirement with session renewal and CSRF protection
- Centralized housekeeping for retention cleanup (database + artifacts)
- Docker entrypoint with cgroup-aware worker auto-detection
- Redacted logging for sensitive headers/cookies/tokens

## Runtime Overview
- API/UI: FastAPI app in app/main.py
- Persistence: SQLite database in data/jobs.db
- Background workers: started at app startup
- Housekeeping daemon: runs every hour and applies retention policy

Housekeeping cleanup logic is centralized in app/utils/housekeeping.py:
- cleanup_expired_jobs(keep_days, data_dir, purge_db_func)
- cleanup_job_directory(job_id, data_dir)
- cleanup_orphaned_directories(data_dir, job_exists_func, dry_run=False)

## Environment Variables

### Required
- TUBEYOU_SECRET_KEY: secret used for session/CSRF signing

### Recommended for Production
- TUBEYOU_ADMIN_PASSWORD: admin login password
- TUBEYOU_ADMIN_USER: admin user name (default: admin)

### General Runtime
- LOG_LEVEL: log level (default: info)
- UVICORN_RELOAD: enables reload in run.py when true/1/yes/on
- TZ: timezone for UI timestamp rendering (default: UTC)

### Worker Runtime
- WORKER_TIMEOUT_DL: download timeout in seconds (default: 3600)
- WORKER_TIMEOUT_TC: transcode timeout in seconds (default: 7200)

### Docker Entrypoint / Gunicorn
- HOST: bind host (default: 0.0.0.0)
- PORT: bind port (default: 8000)
- WORKERS: worker count or auto (default: auto)
- MAX_WORKERS: upper clamp for auto workers (default: 8)
- TIMEOUT: gunicorn worker timeout in seconds (default: 60)
- DATA_DIR: data directory inside container (default: /app/data)

## Local Start
Install dependencies and start:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TUBEYOU_SECRET_KEY="change-me"
export TUBEYOU_ADMIN_PASSWORD="change-me"

python run.py
```

Development reload:

```bash
UVICORN_RELOAD=true LOG_LEVEL=debug python run.py
```

Direct Uvicorn (alternative):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker Start
Run with compose:

```bash
docker compose -f docker/docker-compose.yml up -d
```

## Main Routes
- GET /health
- GET /
- GET /settings
- GET /login
- POST /login
- POST /logout
- POST /api/submit
- GET /api/jobs
- GET /api/settings
- POST /api/settings
- GET /download/{job_id}

## Data and Schema Notes
- The application creates and initializes data/jobs.db on startup if it does not exist.
- Schema setup is designed for fresh install bootstrapping (no migration framework).
- Retention cleanup removes completed/errored jobs older than retention_days and deletes matching job artifact directories.
