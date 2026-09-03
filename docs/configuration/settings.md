# Application Settings

Runtime settings live in the SQLite database and are changed in the UI at
**Settings**. They persist across restarts and upgrades with the data volume.

The Settings page has four tabs: **General**, **Security**, **Integrations**, and
**System**.

## General

The **General** tab is split into panels: **Retention**, **Sharing**, **Runtime
limits**, **Downloads**, and **Watermark**.

| Setting | Panel | Key | Range | Default |
|---|---|---|---|---|
| Retention | Retention | `retention_days` | `0`–`365` | `0` (unlimited) |
| Public hostname | Sharing | `public_hostname` | hostname or IP | empty |
| Share link max uses | Sharing | `share_link_max_uses` | `0`–`10000` | `0` (unlimited) |
| Parallel fragments per download | Downloads | `download_concurrent_fragments` | `Automatic` or `1`–`16` | `Automatic` (`0`) |
| Prefer H.264/AAC for max quality (MP4 preset) | Downloads | `download_mp4_preset` | on/off | on |
| Show fetchly watermark | Watermark | `video_watermark` | on/off | on |

**Retention** — days after which a job's files are swept. `0` keeps everything until
you remove it explicitly. See [Storage & Retention](storage.md).

**Prefer H.264/AAC for max quality (MP4 preset)** — prefer H.264/AAC in MP4 so the
result plays in every browser. Off gives the highest available resolution at the cost
of universal playback (VP9/AV1 do not play in Safari/iOS, which breaks the player,
waveform, and trim view).

**Show fetchly watermark** — burns the fetchly logo into the bottom-right corner of
every downloaded video, with the public hostname on a second line once one is set.
Audio-only jobs are unaffected. The badge (logo, drop shadow, hostname) is composited
once per hostname and output size and cached under `data/watermark-cache/`, so the
encode only alpha-blends a still image into the corner. On `medium`/`small` quality
that rides along in the transcode fetchly already runs and costs nothing measurable;
`max` quality is otherwise a pure download and remux, so it gains an x264 pass
(`-preset veryfast -crf 20`, audio stream-copied) that a 4K download will feel. Turn
the switch off to leave `max` downloads untouched. The hostname line is set in the
same Roboto Flex already shipped for the app UI (`app/static/fonts/`), so no system
font package is required; if that file is ever missing, the logo is drawn alone and a
warning is logged.

**Parallel fragments per download** — parallel fragment downloads for DASH/HLS sources;
ignored for progressive single-file downloads. `Automatic` (`0`) sizes the value per download
from the host's CPU quota and free memory, between 2 and 8 fragments, so a small or
currently loaded host backs off on its own. The settings page names the value
Automatic resolves to at that moment. See [Resources](resources.md).

**Share link max uses** — snapshotted onto each link at creation. Changing it never
retroactively re-opens or closes links already handed out. See
[Share Links](../features/sharing.md).

**Public hostname** — a bare hostname or IP (no scheme, port, or path) that share links
are built from. HTTPS is assumed when set. Empty means "use the host of the request
that created the link". See [Reverse Proxy](reverse-proxy.md).

### Runtime limits

These persist in SQLite. Except for the download-worker count, changes apply to new
work immediately.

| Setting | Key | Range | Default |
|---|---|---|---|
| Download workers | `download_worker_count` | `0`–`8` | `0` (automatic; restart required) |
| Download timeout | `download_timeout_minutes` | `1`–`240` min | `60` min |
| Transcode timeout | `transcode_timeout_minutes` | `1`–`480` min | `120` min |
| Maximum input size | `download_max_filesize_gib` | `1`–`100` GiB | `4` GiB |

**Download workers** controls the in-process download pool. It is read when the app
starts, so restart after changing it. The queue and SSE registry are process-local;
Gunicorn itself remains fixed at one process.

## Security

| Setting | Key | Notes |
|---|---|---|
| Admin username | `admin_username` | Normalized on save |
| Admin password | `admin_password_hash` | PBKDF2-HMAC-SHA256; only the hash is stored |
| Enable authentication | `enable_authentication` | Cannot be enabled before credentials exist |
| Session idle timeout | `session_idle_minutes` | `1`–`1440`, default `60` |

!!! warning "No credentials, no authentication"
    A fresh install has no account and authentication is off. Saving a username and
    password is what makes the toggle available. There is no environment variable and
    no bootstrap password.

Details in [Authentication](../security/authentication.md).

## Integrations

### Cookies

Public URLs download signed out. Age-restricted, private, and login-walled content
needs a signed-in browser session, imported per platform — one tile each for YouTube,
TikTok, Instagram, and Facebook.

| Action | What it does |
|---|---|
| **Paste cookies** | Opens a dialog that walks you through copying the cookie request header out of your browser's dev tools, then stores what you paste. It stays open if the paste is rejected, so the text is not lost. |
| **Remove** | Deletes the stored jar for that platform. Shown only while one exists. |

Every tile shows a status badge plus a line describing what is actually stored — cookie
count and domains, expiry, when the jar was last written, and any missing login cookie.
The badge states and the detail chips are documented under
[Platform Cookies → What a tile shows](../features/cookies.md#what-a-tile-shows).

!!! info "A missing or expired jar never fails a download"
    Cookies are validated when they arrive and re-checked on every status read and
    before every download. While a platform's jar is missing, expired, or unusable,
    its downloads simply run signed out — public URLs keep working, gated ones fail
    with a login-required message. Nothing here has an environment variable; jars live
    in `DATA_DIR/cookies/` at mode `0600`.

Full walkthrough, accepted formats, and security notes in
[Platform Cookies](../features/cookies.md).

### Lalal.ai

| Setting | Key | Notes |
|---|---|---|
| Activation key | `lalalaai_auth_key` | Stored as a secret; never returned by `GET /api/settings` |
| Account email | `lalalaai_email` | Filled from the validated account |
| Validation state | `lalalaai_auth_is_valid`, `lalalaai_auth_checked_at`, `lalalaai_auth_last_error` | Maintained automatically; cached 5 minutes |
| Limit long tracks | `lalalaai_duration_guard` | On by default — blocks tracks over 10 minutes |
| BPM analysis track limit | `audio_analysis_max_minutes` | `0`–`240` min, default `15`; `0` analyzes tracks of any length |
| BPM analysis timeout | `audio_analysis_timeout_minutes` | `1`–`60` min, default `5`; a run that exceeds it is terminated and the job still finalizes |
| Lalal result limit | `lalal_max_download_gib` | `1`–`100` GiB, default `4`; caps stem files fetched back from Lalal.ai |

The three limits sit in the **Analysis & stem limits** section of the Lalal.ai tile.
See [Stem Separation](../features/stems.md).

## System

### Information and maintenance

Read-only panels plus two destructive actions.

| Panel | Shows |
|---|---|
| Version | Running fetchly version and the latest published release |
| Components | Installed vs. upstream versions of yt-dlp, ffmpeg, deno, and friends |
| Host resources | Storage, CPU, RAM, and uptime of the host |
| Changelog | The bundled `CHANGELOG.md`, rendered |

| Action | Endpoint | Rate limit |
|---|---|---|
| Reset statistics | `POST /api/stats/reset` | 5/minute |
| Remove all jobs | `POST /api/jobs/remove-all` | 2/minute |

**Reset statistics** stamps a marker rather than deleting rows, so counters restart
without losing job history. **Remove all jobs** deletes every job row *and* its files —
there is no undo, and share links to those jobs stop working immediately.

### Update checks

`GET /api/updates` compares each component against the source the image actually
installs it from — GitHub releases for the packages, the rolling BtbN build for ffmpeg.
Answers are cached for 24 hours, in memory and in `update_check.json` on the data
volume, so a page reload never triggers another network round trip.

!!! info "Update check unavailable"
    If the fetchly release check reports as unavailable, the GitHub release source
    could not be read. Component checks (yt-dlp, ffmpeg, …) are independent and keep
    working.

## The settings API

```http
GET  /api/settings      # 60/minute
POST /api/settings      # 5/minute
```

- Only a fixed allow-list of keys is writable; unknown keys are rejected
- Every value is parsed and range-checked server-side, not just in the browser
- Internal keys (`admin_password_hash`, `session_version`, `statistics_reset_at`) are
  never writable through the API
- Secret keys (`lalalaai_auth_key`) are never returned by `GET`

See [API Reference](../api/overview.md).
