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


<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-08-31

- ``New`` Initial Release

</details>
