## [1.1.1] - 2026-09-02

- ``New`` Tune the download engine from the app instead of environment variables: download workers, download and transcode timeouts, and maximum input size under Settings → General → Runtime limits. A changed worker count applies after the next restart, the other limits apply to new jobs right away.
- ``New`` Set the BPM analysis track limit and timeout and the Lalal.ai result size limit in the Lalal.ai tile under Settings → Integrations; a track limit of 0 analyzes tracks of any length.
- ``New`` Full documentation at <https://gill-bates.github.io/fetchly> covering installation, configuration, features, the API, security, and troubleshooting.
- ``Fix`` Retention and max-uses-per-share-link now save as soon as the slider is released and confirm the change.
- ``Fix`` Cookie tiles list their details as separate chips (cookie count and domain, expiry, last update), and badges that carry information rather than a status now share one consistent style.
- ``Security`` Session cookies stay marked secure when fetchly runs behind an HTTPS reverse proxy that forwards plain HTTP.
- ``Security`` The stored cookie folder `data/cookies/` is owner-only, and an existing folder left readable by others is tightened on startup.

<details markdown="1">
<summary>Previous versions...</summary>

## [1.1.0] - 2026-09-01

- ``New`` Create and manage the admin account in Settings → Security, then enable login when needed; authentication is off by default.
- ``New`` Configure a public hostname or IP for HTTPS share links, with detection from the current browser address.
- ``New`` Keep job files indefinitely (the new default) or for up to one year, and permanently remove all jobs and their share links from Settings.
- ``New`` View download-volume space, CPU, memory, uptime, and current or previous release notes in Settings → System.
- ``New`` Connect YouTube, TikTok, Instagram and Facebook cookies from Settings → Integrations by pasting them straight from the browser's dev tools; each tile reports how long the stored cookies stay valid.
- ``Fix`` BPM detection no longer depends on madmom: its beat-smoothing step tracked downbeats fetchly discards and changed nothing about the median-interval tempo, while costing an unpinned source dependency last released in 2018.
- ``Fix`` Job cancellation, disabling authentication, resetting statistics, and removing all jobs now use consistent in-app confirmation dialogs instead of browser popups.
- ``Fix`` Platform cookie files are managed entirely under Settings → Integrations and stored in their own `data/cookies/` folder, created automatically on first start; the `FETCHLY_COOKIES_DIR` variable and the top-level data-directory fallback are gone.
- ``Fix`` Downloads now skip an expired cookie file and fall back to an anonymous request instead of sending a stale session; the login-required message tells you whether to add or refresh cookies.
- ``Fix`` Settings fields stay editable and keep focus while a change is saving in the background.

## [1.0.1] - 2026-09-01

- ``New`` Retry a failed or cancelled job in one click, from the job list or its details, without creating a duplicate.
- ``New`` Download a selected audio range directly, or send it straight to vocal and instrumental separation; the trim view now opens with a selection ready and no separate confirmation step.
- ``New`` Live download progress with percentage and ETA in the status pill.
- ``New`` Status pill now names the actual phase (Queued, Downloading, Transcoding, Analyzing) instead of a generic "Running".
- ``New`` Waveform zoom follows the pointer, pans with Shift or drag, and works by touch on phones.
- ``Fix`` Trimming waveform no longer blanks out or jumps while zooming or adjusting a selection, and downloads and separation always use the current selection.
- ``Fix`` Audio range downloads now start reliably on iOS Safari.
- ``Fix`` Dashboard stats no longer fail to load when the tracked totals land on a whole number of minutes.
- ``Fix`` Login form no longer stays disabled after returning to it with the browser back button.
- ``Fix`` Update check reports the latest version from release tags, so it no longer shows "unavailable" right after a new release.
- ``Fix`` Running downloads and live updates now stop promptly when the server restarts.
- ``Security`` Update checks reject oversized upstream responses and corrupted or oversized cache files.
- ``Security`` Outbound thumbnail fetches are validated with the same URL parser that issues the request, closing host allowlist edge cases.
- ``Security`` Turning off authentication now requires an explicit confirmation.

## [1.0.0] - 2026-08-31

- ``New`` Initial Release

</details>
