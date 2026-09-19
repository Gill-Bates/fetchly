<!-- Parent: ../AGENTS.md -->

# config/ — Configuration Files

**Purpose:** Reserved for static configuration templates; fetchly's actual configuration lives in the database and in environment variables.

fetchly uses a hybrid configuration model: most settings (Lalal.ai credentials, retention, watermark preferences, admin account, concurrency tuning) are stored in the SQLite `settings` table so they survive restarts and can be edited in the UI. Server-level configuration is environment-variable-driven. This directory currently holds nothing but its own AGENTS.md.

## Key Files

None. Add `.example` templates here only if a genuinely file-based setting ever appears; prefer database settings.

## For AI Agents

### Understanding fetchly's Configuration Model
- **Database-driven:** runtime settings live in `${DATA_DIR}/jobs.db` → `settings` table (key-value, values stored as strings)
- **Environment variables:** server-level config (secret key, data directory, bind address, proxy trust, Gunicorn tuning)
- **Startup defaults:** `app/db.py` holds a module-level defaults dict; any key missing from the table falls back to it. There is no `_initialize_default_settings()` function.
- **User-editable:** the Settings UI writes to the database; no restart needed

### Adding a New Configuration Option
1. **Default:** add the key and its default string value to the defaults dict in `app/db.py`, plus a coercion entry if it needs one (`_parse_bool`, `_parse_bounded_int`, …)
2. **API:** `/api/settings` (GET) and `/api/settings` (POST) in `app/routes/api.py` already read and write the whole settings map — a new key usually needs no new endpoint
3. **UI:** add the control to `app/templates/settings.html`
4. **Test:** extend `tests/test_runtime_settings.py`

### Environment Variables
`docs/configuration/environment.md` is the authoritative reference. Summary of the ones agents touch most:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FETCHLY_SECRET_KEY` | (required) | Signs session cookies and the anti-bot token; app refuses to start without it |
| `DATA_DIR` | `data/` locally, `/app/data` in Docker | SQLite DB, downloads, cookie jars, thumbnail and model caches |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port; also probed by the container health check |
| `WORKERS` | `1` | Gunicorn processes — **must remain `1`** |
| `FETCHLY_BEHIND_HTTPS` | `0` | Set to `1` behind TLS so session cookies are marked `Secure` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Comma-separated trusted proxy IPs or CIDRs |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, or `error` |
| `TORCH_HOME` | `${DATA_DIR}/.cache/torch` | beat-this checkpoint cache; keep it on the volume |
| `TZ` | `Etc/UTC` | Timezone for UI timestamps and logs |

!!! danger "`WORKERS` must stay at 1"
    The job queue is an in-process `queue.Queue` (`app/worker.py`). A second Gunicorn
    worker gets its own queue and its own governor, and jobs are silently split across
    processes that cannot see each other's state. See `docker/entrypoint.sh`.

There is no `FETCHLY_PUBLIC_URL`, no `FETCHLY_DATA_DIR`, no `FETCHLY_TRUSTED_PROXIES`, no `THREADS` and no `GUNICORN_WORKERS`. Public share URLs are built from the incoming request plus the `public_hostname` database setting (`app/utils/public_url.py`).

### Configuration at Runtime (Database)
Real keys from the defaults dict in `app/db.py`:

| Setting | Example | Purpose |
| --- | --- | --- |
| `enable_authentication` | `false` | Whether login is required at all |
| `login_required` | `false` | Whether anonymous read access is blocked |
| `admin_username` | `""` | The single admin account name |
| `session_idle_minutes` | `60` | Idle timeout before a session expires |
| `retention_days` | `0` | Auto-delete finished jobs after N days (`0` = keep) |
| `download_worker_count` | `0` | `0` means auto-size from the host's CPU quota |
| `download_concurrent_fragments` | `0` | `0` means auto-size per download |
| `download_timeout_minutes` | `60` | Abort a download that exceeds this |
| `transcode_timeout_minutes` | `120` | Abort a transcode that exceeds this |
| `download_max_filesize_gib` | `4` | Reject downloads above this size |
| `download_compatible_output` | `false` | Force a broadly playable container/codec |
| `video_watermark` | `true` | Overlay the watermark on video output |
| `audio_analysis_max_minutes` | `15` | Skip analysis for longer audio |
| `audio_analysis_timeout_minutes` | `5` | Abort analysis that exceeds this |
| `lalalaai_email` / `lalalaai_auth_key` | `""` | Lalal.ai credentials |
| `lalalaai_minutes_left` | `-1` | Cached quota; `-1` = not known yet |
| `lalal_max_download_gib` | `4` | Cap on stem download size |
| `share_link_max_uses` | `0` | `0` = unlimited |
| `public_hostname` | `""` | `""` = use the host of the request that created the link |

### When to Use Config vs Code
- **Config:** user-facing toggles, API credentials, resource limits, retention
- **Code:** business logic, validation rules, error handling, algorithmic behavior
- **Hybrid:** resource limits — configurable, with safe auto-sizing defaults in code

## Dependencies

### Internal
- `../app/db.py` — settings table, defaults dict, coercion helpers
- `../app/main.py` — reads environment variables at import/startup
- `../app/routes/api.py` — `/api/settings` GET and POST

### External
- **Environment:** Docker/systemd/shell sets env vars at runtime
- **Database:** SQLite via the stdlib `sqlite3` module

## Manual

### Viewing Current Configuration
```bash
# Via database (local default path)
sqlite3 data/jobs.db "SELECT key, value FROM settings LIMIT 40"

# Via API (requires an authenticated session when auth is enabled)
curl --cookie "$COOKIE" http://127.0.0.1:8000/api/settings
```

### Changing Configuration
**Option 1: UI (recommended)**
- Log in as admin → Settings → change → Save. Effective immediately.

**Option 2: Direct database**
```bash
sqlite3 data/jobs.db "UPDATE settings SET value='false' WHERE key='video_watermark'"
# Restart to be safe; several settings are read per request, but not all.
```

**Option 3: Environment variables (startup only)**
```bash
export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
export DATA_DIR=/srv/fetchly/data
python run.py
```

### Exporting Configuration for Backup
```bash
sqlite3 data/jobs.db "SELECT json_object('key', key, 'value', value) FROM settings" > settings-backup.json
cp data/jobs.db data/jobs.db.backup
```

### Resetting to Defaults
```bash
sqlite3 data/jobs.db "DELETE FROM settings"
python run.py   # defaults dict repopulates the table on startup
```

---

**Last Updated:** 2026-09-19  
**Configuration Style:** Hybrid (environment + database + runtime UI)  
**Persistence:** Database (survives restarts) + environment (startup only)  
**Database File:** `${DATA_DIR}/jobs.db`
