# Endpoints

Every route, grouped by family. All limits are per client IP.

!!! info "Generated schema"
    FastAPI publishes the live schema at `/openapi.json`, with Swagger UI at `/docs`.
    Once authentication is on, those are gated like everything else.

## Health

### `GET /health`

Unauthenticated liveness probe. Used by the container health check.

```json
{"status": "ok"}
```

## Pages

| Route | Limit | Description |
|---|---|---|
| `GET /` | — | Dashboard |
| `GET /settings` | — | Settings page |
| `GET /login` | 20/min | Login page (sets the CSRF cookie, embeds the anti-bot token) |
| `GET /job/{job_id}` | 120/min | Job detail page: player, waveform, metadata |
| `GET /favicon.ico` | — | Favicon |

## Authentication

| Route | Limit | Body |
|---|---|---|
| `POST /login` | 5/min | JSON: `username`, `password`, `captcha_token`, `honeypot` |
| `POST /logout` | 20/min | — |

See [API Authentication](authentication.md).

## Jobs

### `POST /api/submit`

Form-encoded, CSRF-protected. 10/minute.

| Field | Values |
|---|---|
| `url` | A YouTube, TikTok, Instagram, or Facebook URL |
| `type` | `audio`, `video` |
| `quality` | `max`, `medium`, `small` |
| `confirm_duplicate` | `true` to accept a duplicate |

Returns the created job. `409` with `{"detail": "duplicate_job", "existing_job": {...}}`
when an active or completed job already matches. `503` when the queue is full.

The metadata probe runs with an 8 s budget and fills `video_title` and
`duration_seconds` on the new row, so the job carries a title and a length before the
download starts. A probe that fails or times out leaves both `null` and the job is
still created.

### `GET /api/jobs`

60/minute. Query: `offset` (≥ 0, default 0), `limit` (1–100, default 50).

Returns an array of job objects:

```json
[
  {
    "id": "0f5c...", "url": "https://...", "platform": "youtube",
    "video_title": "...", "video_meta_hover": "...",
    "type": "audio", "quality": "max", "status": "analysis_done",
    "created_at": "2026-09-01 12:00:00", "finished_at": "2026-09-01 12:01:14",
    "message": null, "filesize_bytes": 5242880, "duration_seconds": 214,
    "codec": "mp3", "bitrate_kbps": 192,
    "bpm": 128, "bpm_confidence": 0.87,
    "audio_hash": "...", "filename": "Some Track.mp3"
  }
]
```

### `GET /api/jobs/{job_id}`

60/minute. One job, same shape. `404` if unknown.

### `POST /api/jobs/{job_id}/cancel`

30/minute. Terminates the running subprocess.

### `POST /api/jobs/{job_id}/retry`

10/minute. Allowed only from `error` and `cancelled` — retrying an in-flight or
successful job would race the worker or duplicate a finished download. The **existing
row is reset in place**, keeping its id and its position in the list, and every result
field from the failed attempt (message, filesize, codec, …) is cleared so no stale data
shows while the retry runs.

### `POST /api/jobs/remove-all`

2/minute. Deletes every job row and its artifacts. No undo.

### `GET /api/info`

20/minute. Query: `url`. Resolves title, duration, and thumbnail before submitting.

## Events (SSE)

| Route | Limit | Stream |
|---|---|---|
| `GET /events` | 30/min | All job state changes |
| `GET /api/jobs/{job_id}/events` | 30/min | One job |

`text/event-stream`, keep-alive every 5 s, 32-event queue per client, 200 concurrent
connections maximum.

## Media

| Route | Limit | Description |
|---|---|---|
| `GET /download/{job_id}` | 30/min | The finished file; the name carries the BPM tag when known |
| `GET /audio-source/{job_id}` | 60/min | Audio stream for the player and waveform |
| `GET /thumbnail/{job_id}` | 120/min | The job's thumbnail |
| `GET /api/thumbnail/resolve` | 30/min | Resolve a media URL to a cached thumbnail URL |
| `GET /api/thumbnail-cache/{cache_key}` | 120/min | Serve a cached thumbnail |
| `GET /api/thumbnail-proxy` | 30/min | Proxy a remote thumbnail |

Thumbnail routes validate the content type against the actual bytes, and only fetch
from allow-listed hosts.

## Trimming

| Route | Limit | Description |
|---|---|---|
| `POST /api/trim/{job_id}` | 10/min | Body: `start`, `end` (seconds, float) |
| `DELETE /api/trim/{job_id}` | 30/min | Remove the job's trim outputs |
| `GET /api/trim/{job_id}/{trim_id}/download` | 30/min | Download one trim |

Audio jobs only, downloadable status, 1 second to 10 minutes. `trim_id` is
`<start_ms>_<end_ms>`. See [Audio Trimming](../features/trimming.md).

## Lalal.ai

| Route | Limit | Description |
|---|---|---|
| `GET /api/lalal/status` | 30/min | Auth and validation state plus `remaining_minutes` (`null` when unknown); `force_refresh=true` re-checks |
| `POST /api/lalal/auth/activation-key` | 5/min | Store and validate an activation key |
| `POST /api/lalal/auth/logout` | 10/min | Clear key, email, and cached validation |
| `POST /api/lalal/{job_id}` | 5/min | Query: `stem` (`vocals`/`instrumental`), `trimmed`, `trim_id` |
| `GET /api/lalal/download/{job_id}` | 10/min | Query: `stem` |

Lalal.ai always performs a vocals/instrumental split; `stem` selects which result comes
back. See [Stem Separation](../features/stems.md).

## Cookies

| Route | Limit | Description |
|---|---|---|
| `GET /api/cookies` | 30/min | Validity snapshot for every platform |
| `POST /api/cookies/{platform}` | 10/min | `multipart/form-data` upload of a jar |
| `POST /api/cookies/{platform}/paste` | 10/min | JSON `{"text": "..."}` — cURL, fetch, header, or JSON export |
| `DELETE /api/cookies/{platform}` | 10/min | Remove that platform's jar |

`platform` is `youtube`, `tiktok`, `instagram`, or `facebook`. Max 256 KiB, UTF-8. See
[Platform Cookies](../features/cookies.md).

## Share links

| Route | Limit | Description |
|---|---|---|
| `POST /api/share/{job_id}` | 20/min | Create or reuse a link |
| `GET /share/{token}` | 20/min | **Public.** Redeem a link |

```json
{"ok": true, "url": "https://.../share/aB3xY7_q", "max_uses": 5}
```

Every redeem failure returns the same `404` page. See
[Share Links](../features/sharing.md).

## Settings

| Route | Limit | Description |
|---|---|---|
| `GET /api/settings` | 60/min | Current settings; secrets excluded |
| `POST /api/settings` | 5/min | Write an allow-listed subset |

Writable keys: `retention_days`, `enable_authentication`, `admin_username`,
`admin_password`, `session_idle_minutes`, `download_concurrent_fragments`,
`download_mp4_preset`, `video_watermark`, `lalalaai_duration_guard`,
`share_link_max_uses`, `public_hostname`.

- Unknown keys are rejected
- Values are parsed and range-checked server-side
- `download_concurrent_fragments` accepts `0` for automatic host-based sizing
- `admin_username` and `admin_password` must be sent as a **pair** — the salt is
  username-derived, so a rename re-hashes and needs the plaintext
- Enabling authentication without stored credentials returns `400`
- Internal keys (`admin_password_hash`, `session_version`, `statistics_reset_at`) are
  never writable

## Statistics and system

| Route | Limit | Description |
|---|---|---|
| `GET /api/stats` | 30/min | Aggregate dashboard counters (short-TTL cache) |
| `POST /api/stats/reset` | 5/min | Stamp a reset marker; job history is kept |
| `GET /api/stats/bpm-clusters` | 30/min | `limit` 1–2000; 5-BPM buckets, count-sorted |
| `GET /api/updates` | 30/min | Version check for fetchly and bundled components (24 h cache) |
| `GET /api/system/host` | 60/min | Host storage, CPU, RAM, uptime |

## Error shape

```json
{"detail": "Invalid type. Allowed: audio, video"}
```

| Code | Meaning |
|---|---|
| `400` | Validation error |
| `401` | Invalid credentials or no session |
| `403` | CSRF failure or forbidden |
| `404` | Not found (or deliberately indistinguishable) |
| `409` | Duplicate job |
| `413` | Payload too large |
| `429` | Rate limited |
| `503` | Queue full |
