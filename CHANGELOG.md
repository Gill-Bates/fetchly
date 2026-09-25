
## [1.3.1] - 2026-xx-xx

- ``New`` **Session lifetime** in Settings → Security is set in days (1-7, default 7) and replaces the former idle timeout in minutes. A login now stays valid for the configured number of days counted from sign-in, and is invalidated once it elapses regardless of activity.
- ``Fix`` Stem separation no longer aborts a split on a single dropped connection or timeout while polling Lalal.ai for progress; it retries for about 45 seconds before giving up.
- ``Security`` A login is no longer signed out early after a period of inactivity, and the previous 24-hour hard limit is gone. A session now lives for the full configured **Session lifetime** (up to 7 days, default 7) regardless of activity, which also extends how long a stolen session cookie stays usable. Change the admin password to invalidate every outstanding session immediately.
- ``Security`` Logging out now revokes the session server-side instead of only clearing the cookie, so a copied session cookie stops working the moment its owner signs out.


<details markdown="1">
<summary>Previous versions...</summary>

## [1.3.0] - 2026-09-21

- ``New`` A three-way video output slider (Source, H.264 (Recommended), or AV1) keeps matching source codecs without re-encoding; existing settings are preserved and changes apply to new downloads only. A new **Enable Job History** setting omits newly submitted jobs from the dashboard list, while retention now removes expired jobs completely, including files, history entries, share links, and statistics.
- ``Fix`` Source-mode watermarking preserves the source codec (VP9, AV1, HEVC, etc.), and unlimited retention (`0` days) skips housekeeping entirely.
- ``Fix`` Statistics tiles refresh immediately after Reset Statistics or Remove all Jobs; stale asynchronous status responses can no longer overwrite newer Lalal.ai, watermark-logo, or cookie state; watermark-logo uploads and removals are serialized; and the update checker clears an update notice when a refresh finds none.
- ``Fix`` Every container rebuild fetches the current wavesurfer.js release, keeps application files readable by the unprivileged user, and refreshes package indexes so published images include current Debian security updates.
- ``Security`` Docker Compose binds the published port to `127.0.0.1` by default, keeping an instance without an admin account off the network; set `FETCHLY_BIND=0.0.0.0` to expose it.
- ``Security`` The container rejects system directories such as `/etc` and `/` as `TORCH_HOME`, preventing ownership or permission changes outside the cache directory.
- ``Security`` Release builds take every dependency except the PyTorch CPU packages from PyPI alone, so no other package can be pulled from the PyTorch wheel index.

## [1.2.2] - 2026-09-20

- ``New`` Switched from AGPL-3.0 to MIT license.
- ``New`` Every page now has a proper heading (Dashboard, Settings, Job Status, Sign in) for screen readers and "jump to heading".
- ``New`` Static files are versioned by content hash, so a cached page never keeps serving an outdated stylesheet or script.
- ``New`` A "Skip to main content" link appears on first Tab press, so keyboard users can jump past the navbar.
- ``Fix`` The Lalal.ai balance no longer shows the previous account's minutes after you save a different activation key; disconnecting clears it.
- ``Fix`` An unreachable Lalal.ai is reported as temporarily unavailable instead of marking your activation key invalid.
- ``Fix`` "Remove all" no longer freezes the app while stopping running downloads.
- ``Fix`` You are no longer signed out early after visiting a page whose address merely starts with `/download`, `/thumbnail` or `/static`.
- ``Fix`` `Automatic` download workers cap at 4 instead of 8; more threads added database contention, not speed.
- ``Fix`` An unreadable watermark logo is rejected with a proper message instead of an upload error.
- ``Fix`` On touch tablets up to iPad Pro 12.9" in landscape, the job list uses the compact card layout instead of a cramped table.
- ``Fix`` On touch devices, the navbar icons (Back, Settings, Logout) are full 44px tap targets; on tablets they were 4px short of the minimum.
- ``Fix`` On touch devices, tapping the logo on a sub-page takes you home again; the pulse animation stays on the dashboard.
- ``Fix`` On phones, the stat tiles caption their numbers ("Disk free", "CPU", "Memory", "Uptime") instead of showing only an icon.
- ``Fix`` Retrying a job you just cancelled no longer flips straight back to "Cancelled" when the old attempt finishes shutting down.
- ``Fix`` A track with no detectable tempo is remembered as such, so downloading the same audio again no longer repeats the full BPM analysis.
- ``Fix`` Restarting no longer waits for a running BPM analysis to finish; the analysis is stopped and picked up again on the next start.
- ``Fix`` Downloads and the trim/stem/preview conversions now share one transcode budget instead of each getting the full limit, which could run twice as many encoders as configured.
- ``Fix`` Video jobs show a plain Download button instead of a dropdown whose only entry was "Download"; the audio menu drops that duplicate too and starts at Trim.
- ``Fix`` A track reported as 0 seconds long now counts as unknown, so the Duration Guard blocks stem separation on it instead of letting the request through.
- ``Security`` Share links now use 128-bit tokens instead of 48-bit. Existing links keep working.
- ``Security`` The container image applies current OS security updates at build time.
- ``Security`` TikTok thumbnail lookups no longer follow redirects.
- ``Security`` Rate limiting rejects malformed `X-Forwarded-For` entries instead of trimming them into a usable address.
- ``Security`` Cookie file names must resolve inside the cookies folder, so no lookup can reach a path outside it.
- ``Security`` The public hostname rejects over-bracketed IPv6 input such as `[[::1]]` instead of quietly accepting it.
- ``Security`` Video metadata larger than 16 MiB is discarded instead of parsed, so an oversized response from a source cannot stall a preview.

## [1.2.1] - 2026-09-04

- ``New`` On phones and tablets, a "Current job" card keeps the download you just started visible even with Job History collapsed; it rejoins the list once the screen is wide enough.
- ``New`` Upload your own SVG or transparent PNG as the video watermark instead of the fetchly logo (Settings → Processing → Watermark), with a live preview and a one-click reset to the built-in artwork. The server validates format, size, proportions and transparency.
- ``New`` `max` quality now keeps the source untouched: the highest available resolution is muxed into its native container (`.webm`, `.mkv`) with no re-encoding. Downloads that will not play on Safari, iOS or most TVs are marked "Limited playback" in the job list and job details.
- ``New`` "Prefer H.264/AAC for max quality" becomes "Universally playable output (H.264/AAC)" and now guarantees a compatible format, re-encoding only when the source is not already compatible. Existing installations keep their previous setting; enabling the watermark locks this option on.
- ``Fix`` With the watermark enabled, `max` downloads are no longer softer than the source: the encoder now derives its quality from the source resolution. Turning the watermark off still skips re-encoding entirely.
- ``Fix`` Downloads with a very long or non-Latin (e.g. CJK, Cyrillic) title no longer fail with a filesystem name-length error.
- ``Fix`` Fetching a video's title now fails fast instead of tying up a worker slot when the source is blocked or unreachable.
- ``Fix`` A worker thread that fails to stop in time on restart is no longer left behind, which could otherwise run a second, overlapping set of download workers.
- ``Security`` Error messages and logs from failed downloads no longer contain signed access tokens from CDN URLs.

## [1.2.0] - 2026-09-03

- ``New`` Optional fetchly watermark on downloaded videos (Settings → General → Downloads), on by default; free for capped qualities, adds an encoding pass on `max`.
- ``New`` Parallel download fragments now have an `Automatic` mode that picks 2-8 from host CPU and memory; 1-16 fixed still available.
- ``New`` Track length shown in the job list, detail dialog, and job page, updating while the download runs.
- ``New`` Remaining Lalal.ai processing balance shown in Settings → Integrations.
- ``New`` Configurable session idle timeout in Settings → Security (1-1440 min).
- ``New`` Details button added to the download options menu.
- ``New`` On phones the four stat tiles moved from above the download form to the top of Settings → System.
- ``New`` The job list marks the media type with a movie or music icon instead of the words "Video"/"Audio".
- ``Fix`` Audio rows no longer show a redundant quality label; audio always downloads the best available quality.
- ``Fix`` Repeating the same notification no longer stacks copies in the corner; the visible one's dismiss timer just restarts.
- ``Fix`` Fewer "database is locked" errors under heavy concurrent use.
- ``Fix`` The video watermark renders reliably when the bundled font check would previously have raced and skipped it.
- ``Fix`` Unknown track lengths show clearly instead of `0:00` or a blank field.
- ``Fix`` Trim view no longer draws a second, cut-off waveform, and the playback cursor and progress highlight are back.
- ``Fix`` Changing the public hostname now confirms the save.
- ``Fix`` Settings and Lalal.ai errors always show a readable message.
- ``Fix`` Cookie tiles state plainly whether signed-in downloads will work ("Ready", "Needs update", "Not set up").
- ``Fix`` On iPad, tapping a Settings field or the job search box no longer zooms the page in with no way back out, and the Settings controls are touch-sized there as they already were on phones.
- ``Security`` Admin username and public-hostname fields reject values with a hidden trailing newline.
- ``Security`` Form submissions on a dropped connection are rejected outright instead of being processed with only part of their data.

## [1.1.1] - 2026-09-02

- ``New`` Tune the download engine from the app instead of environment variables: download workers, download and transcode timeouts, and maximum input size under Settings → General → Runtime limits. A changed worker count applies after the next restart, the other limits apply to new jobs right away.
- ``New`` Set the BPM analysis track limit and timeout and the Lalal.ai result size limit in the Lalal.ai tile under Settings → Integrations; a track limit of 0 analyzes tracks of any length.
- ``New`` Full documentation at <https://gill-bates.github.io/fetchly> covering installation, configuration, features, the API, security, and troubleshooting.
- ``Fix`` Retention and max-uses-per-share-link now save as soon as the slider is released and confirm the change.
- ``Fix`` Cookie tiles list their details as separate chips (cookie count and domain, expiry, last update), and badges that carry information rather than a status now share one consistent style.
- ``Security`` Session cookies stay marked secure when fetchly runs behind an HTTPS reverse proxy that forwards plain HTTP.
- ``Security`` The stored cookie folder `data/cookies/` is owner-only, and an existing folder left readable by others is tightened on startup.

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
