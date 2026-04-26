# tubeyou

`tubeyou` is a small FastAPI app for downloading and converting YouTube videos. It keeps a SQLite job history, shows video metadata in the UI, supports login protection, and includes a settings page for retention and integration keys.

## Features
- YouTube URL validation and job submission
- Video download, transcode, and MP3 output support
- Job list with status, codec, bitrate, duration, and file size
- Video title display with hover metadata
- Login protection and CSRF-safe settings updates
- Automatic retention cleanup for finished jobs
- Docker-friendly startup and redacted logs

## Environment Variables
- `TUBEYOU_SECRET_KEY` - required; session and CSRF signing secret
- `TUBEYOU_ADMIN_PASSWORD` - required outside debug mode; admin login password
- `TUBEYOU_ADMIN_USER` - optional; defaults to `admin`
- `LOG_LEVEL` - optional; `debug` enables reload mode, default is `info`
- `TZ` - optional; local time zone for timestamps, default is `UTC`

## Start
Local:
```bash
source .venv/bin/activate
export TUBEYOU_SECRET_KEY="..."
export TUBEYOU_ADMIN_PASSWORD="admin"
python run.py
```

Direct Uvicorn:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Development reload:
```bash
LOG_LEVEL=debug python run.py
```

Docker:
```bash
docker compose up -d
```

## Notes
- The app creates a fresh SQLite database at `data/jobs.db`.
- No migration backfills are used; the schema is created for a new database on startup.
