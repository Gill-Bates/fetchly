# Docker Setup

Everything specific to running fetchly as a container.

## Image layout

The image is built in stages so the runtime layer carries only what the app needs:

| Stage | Produces |
|---|---|
| `ffmpeg` | A static ffmpeg/ffprobe build |
| `essentia` | Essentia, compiled from source on every architecture from one pinned upstream commit |
| `builder` | The Python virtualenv and the vendored wavesurfer.js bundle |
| `runtime` | The final image |

Application code is copied **root-owned and read-only**. The container starts as root
so the entrypoint can fix ownership of a mounted `/app/data`, then drops to the
unprivileged `appuser` (UID 1000) via `gosu` before exec'ing Gunicorn. A compromised
application process cannot rewrite its own Python modules, templates, or JavaScript —
only `/app/data` is writable.

## Volume layout

Mount one volume at `/app/data`. Everything that survives a container recreation lives
under it:

```text
/app/data/
├── jobs.db               # SQLite database (WAL mode): jobs, settings, share links
├── <job-uuid>/           # One directory per job: media, thumbnail, trims, stems
├── cookies/              # Imported per-platform cookie jars
├── thumb-cache/          # Cached remote thumbnails
├── watermark-cache/      # Rendered video watermark badges
├── update_check.json     # Cached upstream release check (24 h)
└── .cache/torch/         # beat_this model checkpoint (~81 MB)
```

!!! danger "The data volume is sensitive"
    `cookies/` holds live, signed-in browser sessions for your own platform accounts,
    and `jobs.db` holds your Lalal.ai key and the admin password hash. Treat the
    volume with the same care as a credentials store: restrictive permissions, and
    encrypted backups.

## Container environment

The image and its entrypoint apply these defaults; override them only when you have a
reason:

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `/app/data` | Use an absolute path |
| `HOST` | `0.0.0.0` | Bind address inside the container |
| `PORT` | `8000` | Also probed by the health check |
| `WORKERS` | `1` | **Must remain 1** — see below |
| `TIMEOUT` | `60` | Gunicorn worker timeout |
| `GRACEFUL_TIMEOUT` | `15` | Shutdown budget before SIGKILL |
| `ACCESS_LOG_FORMAT` | `[%(t)s] %(h)s "%(r)s" %(s)s %(b)s` | Gunicorn access-log format |
| `APP_USER` | `appuser` | Unprivileged account the entrypoint drops to |
| `TORCH_HOME` | `${DATA_DIR}/.cache/torch` | Keeps the model checkpoint on the volume |

`UVICORN_HOST`, `UVICORN_PORT`, and `UVICORN_WORKERS` are accepted as fallbacks for
`HOST`, `PORT`, and `WORKERS`.

The full list of application variables is in
[Environment Variables](../configuration/environment.md). Download workers, timeouts,
input size, and the BPM and Lalal.ai limits are **not** environment variables — they are
set in the UI, see [Application Settings](../configuration/settings.md).

!!! danger "WORKERS must stay at 1"
    The job queue and the SSE subscriber registry live in process memory with no
    cross-process coordination. A second Gunicorn worker means the same job processed
    twice, and clients subscribed to a process that never sees their job's events.
    This is a correctness failure, not a throughput trade-off — the entrypoint
    enforces it. CPU parallelism comes from the Governor's worker threads and
    semaphores instead; see [Resources & Workers](../configuration/resources.md).

## Health check

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "... urlopen('http://127.0.0.1:'+PORT+'/health') ..." || exit 1
```

`GET /health` is unauthenticated by design so orchestrators can probe it, and returns
only `{"status":"ok"}`.

## Hardening

```yaml
services:
  fetchly:
    image: giiibates/fetchly:latest
    restart: always
    stop_grace_period: 20s
    security_opt:
      - no-new-privileges:true
    ports:
      - "127.0.0.1:8000:8000"   # bind to loopback; publish via a reverse proxy
    environment:
      FETCHLY_SECRET_KEY: "${FETCHLY_SECRET_KEY:?required}"
      FETCHLY_BEHIND_HTTPS: "1"
      FORWARDED_ALLOW_IPS: "172.18.0.0/16"
      TZ: "${TZ:-Etc/UTC}"
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
    volumes:
      - ./data:/app/data
```

!!! tip "Read-only root filesystem"
    `read_only: true` works if you also mount `tmpfs` at `/tmp` — ffmpeg and the
    cookie importer write temporary files there.

`stop_grace_period` must stay above `GRACEFUL_TIMEOUT` so the SQLite WAL checkpoint on
shutdown actually completes before Docker kills the container.

## Logs

```bash
docker logs -f fetchly
```

Set `LOG_LEVEL` to `debug`, `info`, `warning`, or `error`. The startup banner prints
the resolved version, data directory, worker count, and detected tool versions —
useful as a first stop when something behaves unexpectedly.

## Timezone

`TZ` controls the timestamps rendered in the UI and written to the logs. Job records
themselves are stored in UTC.

```yaml
environment:
  TZ: Europe/Berlin
```

## Backups

Everything is in the volume. Stop the container so the database is checkpointed, then
copy the directory:

```bash
docker compose stop
tar czf fetchly-backup-$(date +%F).tar.gz data/
docker compose start
```

Restore by putting the directory back and starting the container; the schema migrates
forward automatically.
