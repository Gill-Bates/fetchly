# Application Settings

Runtime settings live in the SQLite database and are changed in the UI at
**Settings**. They persist across restarts and upgrades with the data volume.

The Settings page has five tabs: **General**, **Processing**, **Security**,
**Integrations**, and **System**.

## General

The **General** tab is split into panels: **Retention**, **Sharing**, and **Runtime
limits**.

| Setting | Panel | Key | Range | Default |
|---|---|---|---|---|
| Retention | Retention | `retention_days` | `0`–`365` | `0` (unlimited) |
| Enable Job History | Retention | `enable_job_history` | on/off | on |
| Public hostname | Sharing | `public_hostname` | hostname or IP | empty |
| Share link max uses | Sharing | `share_link_max_uses` | `0`–`10000` | `0` (unlimited) |

**Retention** — days after which an expired job is removed completely: its files, its
entry in the job history and its share links. Its numbers leave the statistics with it.
`0` keeps everything until you remove it explicitly. See
[Storage & Retention](storage.md).

**Enable Job History** — whether new jobs are listed in the dashboard's job history.
Switched off, a new job still runs normally and still counts in the statistics; it is
just left out of the dashboard list. The value is snapshotted onto each job when it is
submitted, so the setting applies to **new jobs only**: turning it off does not hide
jobs already in the list, and turning it on does not bring back jobs submitted while it
was off. This is separate from the **Show Job History** toggle on the dashboard, which
only collapses the list in your browser.

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
| Parallel fragments per download | `download_concurrent_fragments` | `Automatic` or `1`–`16` | `Automatic` (`0`) |
| Download timeout | `download_timeout_minutes` | `1`–`240` min | `60` min |
| Transcode timeout | `transcode_timeout_minutes` | `1`–`480` min | `120` min |
| Maximum source download size | `download_max_filesize_gib` | `1`–`100` GiB | `4` GiB |

**Download workers** controls the in-process download pool. It is read when the app
starts, so restart after changing it. The queue and SSE registry are process-local;
Gunicorn itself remains fixed at one process.

**Parallel fragments per download** is yt-dlp's `--concurrent-fragments`, which applies
to fragmented DASH/HLS sources and is ignored for progressive single-file downloads.
`Automatic` sizes it per download from the host's CPU quota and free memory; the UI
shows what that resolves to on this host right now. Higher values use more CPU.

**Maximum source download size** is passed straight through as yt-dlp's own
`--max-filesize` flag. It bounds the size of the file yt-dlp fetches from the
platform (YouTube, TikTok, …), not the size of any file fetchly produces afterwards —
transcoding, trimming, or the watermark pass happen after the download completes and
are not covered by this limit. A source larger than the configured value fails the
download instead of being fetched and then rejected.

## Processing

The **Processing** tab is split into panels: **Output format** and **Watermark**.

| Setting | Panel | Key | Range | Default |
|---|---|---|---|---|
| Video output format | Output format | `download_output_mode` | `source`, `universal`, `av1` | `universal` |
| Show Watermark | Watermark | `video_watermark` | on/off | on |
| Custom logo | Watermark | *(file, not a setting)* | SVG or PNG upload | built-in logo |

**Video output format** — a three-position slider that decides what `max` quality
means. Both targeted modes are preferred at format-selection time, so a source that
already matches is downloaded and remuxed with no encoder involved.

**Source** (left) — the source rendition is passed through untouched: the highest
resolution the source offers, in the container those streams belong in (`.mp4`,
`.webm` for VP9/Opus, `.mkv` for AV1), with nothing lost. No encoder runs at all unless
the watermark is on (see below). Those
files do not play on Safari, iOS or most TVs, so jobs that produce one are marked
**Limited playback** in the job list.

**H.264 (Recommended)** (middle, default; stored as `universal`) — the finished file is
guaranteed H.264/AAC in MP4.
The promise is kept at format selection first (yt-dlp sorts `vcodec:h264` ahead of
resolution), which costs no CPU and no quality — you only give up the resolutions that
exist solely as VP9/AV1. A source with no H.264 rendition at all is re-encoded
afterwards, and only as far as needed: a file whose video is already H.264 but whose
audio is Opus gets `-c:v copy` and an AAC audio track, nothing more.

**AV1** (right) — the finished video is guaranteed AV1, for the smallest file at a
given quality. An AV1 rendition is preferred at format selection, so a source that
already is AV1 is never re-encoded. One that is not goes through `libsvtav1`, which is
considerably slower than H.264 — budget for it before switching. The audio track is
left exactly as downloaded (AV1 is a video codec), and the source container is kept,
since AV1 is valid in `.webm`, `.mkv` and `.mp4` alike.

The setting applies to **new downloads only**. Nothing already on disk is touched:
fetchly never converts a finished file retroactively, so a job downloaded under one
mode keeps that format for good. The mode is read when a download starts, which means a
job still sitting in the queue picks up the new value, and retrying a failed or
cancelled job re-downloads it under the current mode.

Upgraded installs keep their previous behaviour: the old `download_compatible_output`
switch maps to `universal` when it was on and `source` when it was off (and an install
still on the older `download_mp4_preset` migrates the whole way in one start).

The watermark burns an overlay into the picture, which costs a video encode in every
mode — but the encode targets the mode's own codec, so the watermark never changes the
format you chose. Under **Source** it re-encodes into the codec the download arrived in
(H.264, HEVC, AV1, VP9 or VP8) and keeps the container; the file is then no longer a
bit-exact copy of the source, but it is still the same format. A codec fetchly has no
encoder for falls back to H.264 in MP4.

See [Video Codecs & Output Format](../features/video-codecs.md) for the full
trade-off between resolution, file size, compatibility and CPU time, and
[Downloads](../features/downloads.md#video-output-format) for the mechanics.

**Show Watermark** — burns the fetchly logo into the bottom-right corner of
every downloaded video, with the public hostname on a second line once one is set.
Audio-only jobs are unaffected. The badge (logo, drop shadow, hostname) is composited
once per hostname and output size and cached under `data/watermark-cache/`, so the
encode only alpha-blends a still image into the corner. On `medium`/`small` quality
that rides along in the transcode fetchly already runs and costs nothing measurable;
`max` quality is otherwise a pure download and remux, so it gains an encoding pass that
a 4K download will feel. Which encoder runs follows the output format — x264 for
**H.264**, `libsvtav1` for **AV1**, and under **Source** whichever matches the codec
that was downloaded. Each picks its settings from the source resolution — the CRF is the
quality lever, and a small low-bitrate source needs a lower one than a high-bitrate 4K
frame does, which a flat setting got visibly wrong (for x264: `medium`/CRF 16 up to
576p, `fast`/CRF 18 up to 1080p, `veryfast`/CRF 20 above; the other encoders use their
own equivalents, since CRF scales do not carry across). Audio is stream-copied
unless the compatibility promise needs it re-encoded. Turn the switch off to leave
`max` downloads untouched. The hostname line is set in the
same Roboto Flex already shipped for the app UI (`app/static/fonts/`), so no system
font package is required; if that file is ever missing, the logo is drawn alone and a
warning is logged.

**Custom logo** — upload an SVG or a PNG with transparency to replace the fetchly logo
in the badge. The logo in use stays on screen as a small preview; the trash button next
to it restores the bundled artwork. A PNG is stored as you exported it; an SVG is
rasterized in your browser and only the flattened PNG is stored. Either way the server
re-checks format, size, aspect ratio, and transparency and reports which check failed.
The file lives at `data/logo/watermark-logo.png`, and the badge is laid out from your
image's proportions. Full detail in
[Downloads → Your own logo](../features/downloads.md#your-own-logo).

**Parallel fragments per download** — parallel fragment downloads for DASH/HLS sources;
ignored for progressive single-file downloads. `Automatic` (`0`) sizes the value per download
from the host's CPU quota and free memory, between 2 and 8 fragments, so a small or
currently loaded host backs off on its own. The settings page names the value
Automatic resolves to at that moment. See [Resources](resources.md).

## Security

| Setting | Key | Notes |
|---|---|---|
| Admin username | `admin_username` | Normalized on save |
| Admin password | `admin_password_hash` | PBKDF2-HMAC-SHA256; only the hash is stored |
| Enable authentication | `enable_authentication` | Cannot be enabled before credentials exist |
| Session lifetime | `session_max_days` | `1`–`7` days, default `7`; absolute, counted from login |

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
| Host resources | Storage, CPU, RAM, and uptime of the host; on phones each tile is captioned ("Disk free", "CPU", "Memory", "Uptime") |
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

- Only a fixed allow-list of keys is writable; unknown keys are silently ignored
- Every value is parsed and range-checked server-side, not just in the browser
- Internal keys (`admin_password_hash`, `session_version`, `statistics_reset_at`) are
  never writable through the API
- Secret keys (`lalalaai_auth_key`) are never returned by `GET`

See [API Reference](../api/overview.md).
