# Environment Variables

Bootstrap, networking, storage, and process-level integration are configured with
environment variables. With Compose, put them in an `.env` file next to the compose
file.

Everything that a *user* changes at runtime — credentials, retention, share limits, the
runtime limits, the Lalal.ai key — lives in the database instead; see
[Application Settings](settings.md).

!!! info "This page is the complete list"
    Every variable the application, the container entrypoint, and `run.py` read is
    documented below. Anything not listed here has no effect. The README covers only
    the handful an operator normally touches.

## Required

| Variable | Description |
|---|---|
| `FETCHLY_SECRET_KEY` | **Required.** Signs session cookies and the invisible anti-bot token. Generate with `openssl rand -base64 32`. The app refuses to start without it. |

!!! danger "Treat the key as a secret and keep it stable"
    Rotating it invalidates every session and every pending login form. Never commit it
    to a repository or bake it into an image.

## Networking

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port; also probed by the container health check |
| `FETCHLY_BEHIND_HTTPS` | `0` | Set to `1` when serving over HTTPS so session cookies are marked `Secure` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Comma-separated trusted proxy IPs or CIDRs |

!!! warning "`FORWARDED_ALLOW_IPS` and `*`"
    A wildcard is **rejected**. Client IP is what the rate limiter keys on; trusting
    `X-Forwarded-For` from anyone lets a caller spoof their way around every limit. Set
    it to your proxy's address or network. See [Reverse Proxy](reverse-proxy.md).

In the container, `UVICORN_HOST`, `UVICORN_PORT`, and `UVICORN_WORKERS` are accepted as
fallbacks for `HOST`, `PORT`, and `WORKERS`. Prefer the plain names; the aliases exist
for compatibility with images that set them.

## Storage

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `data/` locally, `/app/data` in Docker | Database, downloads, cookie jars, thumbnail cache, model cache. Use an absolute path in Docker. |
| `TORCH_HOME` | `${DATA_DIR}/.cache/torch` | Where the `beat_this` checkpoint is cached. Keep it on the volume so the ~81 MB download survives a container recreate. |

## Runtime

| Variable | Default | Description |
|---|---|---|
| `TZ` | `Etc/UTC` | Timezone for UI timestamps and logs |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, or `error` |
| `WORKERS` | `1` | Gunicorn processes — **must remain `1`** |
| `TIMEOUT` | `60` | Gunicorn worker timeout (container) |
| `GRACEFUL_TIMEOUT` | `15` | Shutdown budget before SIGKILL (container) |
| `ACCESS_LOG_FORMAT` | `[%(t)s] %(h)s "%(r)s" %(s)s %(b)s` | Gunicorn access-log format (container) |
| `APP_USER` | `appuser` | Unprivileged account the entrypoint drops to (container) |
| `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` | `10` | Graceful shutdown budget in seconds for `python run.py` |

!!! danger "`WORKERS` must stay at 1"
    The job queue and the SSE subscriber registry are process-local. A second worker
    means duplicate job processing and clients subscribed to a process that never sees
    their events. CPU parallelism comes from worker threads instead — see
    [Resources & Workers](resources.md).

## Jobs and workers

Download worker count, download and transcode timeouts, and the maximum input size are
configured in **Settings → General → Runtime limits**. The worker-count change applies
after the next restart; the other limits apply to new work immediately.

| Variable | Default | Description |
|---|---|---|
| `WORKER_QUEUE_MAXSIZE` | auto | Queue depth before submissions are rejected with `503` |

## Audio analysis

The maximum track length and processing timeout for BPM analysis are configured in
**Settings → Integrations → Lalal.ai**. A track limit of `0` means unlimited.

## Lalal.ai

The activation key is entered under **Settings → Integrations**. The maximum result
size is configured in the same **Settings → Integrations → Lalal.ai** tile. See
[Stem Separation](../features/stems.md).

## Advanced: the resource governor

Defaults adapt to the detected CPU quota and memory. Change them only with a measured
reason.

| Variable | Description |
|---|---|
| `CPU_SEMAPHORE_LIMIT` | Concurrent CPU-bound operations |
| `ANALYSIS_SEMAPHORE_LIMIT` | Concurrent BPM analyses |
| `IO_SEMAPHORE_LIMIT` | Concurrent I/O-bound operations |
| `TRANSCODE_SEMAPHORE_LIMIT` | Concurrent ffmpeg transcodes |
| `MEMORY_THRESHOLD_MB` | Memory headroom below which backpressure engages |
| `ENABLE_BACKPRESSURE` | Whether to shed load under pressure |

See [Resources & Workers](resources.md).

## Development

Only `python run.py` reads these; the container ignores them.

| Variable | Default | Description |
|---|---|---|
| `UVICORN_RELOAD` | off | Auto-reload on code changes |
| `UVICORN_RELOAD_DIRS` | `app/`, `middleware/` | Comma-separated directories to watch |
| `UVICORN_RELOAD_EXCLUDES` | `${DATA_DIR}`, `.git/` | Comma-separated paths to keep off the watcher |

Watching only the source directories is what keeps `data/` off the raw watcher — an
exclude alone cannot carve a subdirectory back out of a single top-level watch. See
[Development Setup](../development/setup.md).

The startup banner honours the usual console-colour conventions: `NO_COLOR` disables
colour, `FORCE_COLOR` and `CLICOLOR_FORCE` force it on, and `TERM` set to `dumb`
disables it.

## Build-time

| Variable | Description |
|---|---|
| `WAVESURFER_VERSION` | Baked into the runtime image and shown in **Settings → System**. The vendored bundle carries no version marker of its own, so outside the image the value reads `unavailable`. |

## Compose-file variables

These are interpolated by Docker Compose itself and never reach the application. They
are only defined by the shipped [`docker-compose.yml`](https://github.com/Gill-Bates/fetchly/blob/main/docker/docker-compose.yml).

| Variable | Default | Description |
|---|---|---|
| `FETCHLY_TAG` | `latest` | Image tag to pull |
| `FETCHLY_PORT` | `8000` | Host port published to the container |

## Example

```bash title=".env"
# Required
FETCHLY_SECRET_KEY=your-generated-key-here

# Behind a TLS-terminating reverse proxy
FETCHLY_BEHIND_HTTPS=1
FORWARDED_ALLOW_IPS=172.18.0.0/16

# Runtime
TZ=Europe/Berlin
LOG_LEVEL=info

# Runtime download, BPM-analysis, and Lalal limits are configured in the UI.
```

## Precedence

1. Environment variables — operator-level, read at startup
2. Database settings — user-level, changed at runtime in the UI

The two never overlap: no setting is reachable from both sides, so there is nothing to
resolve between them. In particular, **no environment variable can set or override the
admin credentials** or the runtime limits; those exist only in the database.

!!! note "Variables removed in 1.1.1"
    `WORKER_COUNT`, `WORKER_TIMEOUT_DL`, `WORKER_TIMEOUT_TC`, `WORKER_MAX_FILESIZE`,
    `FETCHLY_MAX_ANALYSIS_SECONDS`, `FETCHLY_AUDIO_ANALYSIS_TIMEOUT_SECONDS`, and
    `FETCHLY_LALAL_MAX_DOWNLOAD_BYTES` are no longer read. Their replacements are the
    runtime limits in [Application Settings](settings.md); leaving them in an `.env`
    file is harmless but has no effect.
