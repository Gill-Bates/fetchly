# Job Dashboard

The dashboard is a live view of every download, not a page you reload. Job state is
pushed to the browser over Server-Sent Events.

## Live updates

Two SSE endpoints exist:

| Endpoint | Scope |
|---|---|
| `GET /events` | Every job — powers the dashboard list |
| `GET /api/jobs/{job_id}/events` | One job — powers the job detail page |

| Property | Value |
|---|---|
| Keep-alive interval | 5 s |
| Per-client queue | 32 events |
| Max concurrent connections | 200 |
| Rate limit | 30/minute per client |

!!! warning "The event broker is process-local"
    Subscribers only receive events published by the same Python process. This is why
    `WORKERS` must stay at `1`; with a second Gunicorn worker, clients would subscribe
    to a process that never sees their job. See
    [Resources & Workers](../configuration/resources.md).

If a client's queue overflows — a browser tab suspended in the background, for
instance — the connection is dropped rather than allowed to grow unbounded. The page
reconnects and re-reads the current state from `GET /api/jobs`.

## Job statuses

| Status | Terminal | Meaning |
|---|:---:|---|
| `queued` | | Waiting for a worker slot |
| `processing` | | Metadata extraction and format selection |
| `downloading` | | yt-dlp is fetching |
| `transcoding` | | ffmpeg is converting |
| `analysis` | | BPM detection running; the file is already downloadable |
| `analysis_done` | :material-check: | Finished, with analysis |
| `done` | :material-check: | Finished, no analysis |
| `error` | :material-check: | Failed — the message is shown and a retry is offered |
| `cancelled` | :material-check: | Cancelled by the user |

`done` and `analysis_done` both count as *completed*. `analysis` additionally counts as
*downloadable*, so an audio file can be fetched while its tempo analysis is still
running.

## Job actions

| Action | Availability |
|---|---|
| Download | Any downloadable status |
| Open job page | Any job |
| Cancel | While in flight |
| Retry | After `error` |
| Trim | Audio jobs, downloadable — [Audio Trimming](trimming.md) |
| Stems | Audio jobs, Lalal.ai connected — [Stem Separation](stems.md) |
| Share | Completed jobs — [Share Links](sharing.md) |
| Delete | Any job; removes the row and its artifacts |

## Track length

`duration_seconds` is filled twice. At submit time it comes from the source's own
metadata, so a job shows its length while it is still queued or downloading. When the
download finishes, ffprobe measures the actual file and replaces the value — a probe
that comes back empty leaves the source value in place rather than blanking it.

It is rendered as `M:SS`, or `H:MM:SS` from an hour up, in the job list (desktop media
column and mobile card), the detail dialog, and on the job page. Jobs whose length is
unknown show an en dash.

## The job page

`GET /job/{job_id}` renders a detail view with:

- The **player** (in-browser playback of the downloaded file)
- The **waveform** for audio jobs, with drag-to-select trimming
- Detected **BPM and beat confidence**, when analysis has run
- Job metadata: source URL, platform, type, quality, duration, file size, timestamps

## Statistics

`GET /api/stats` returns aggregate counters for the dashboard — totals by status, bytes
downloaded, and the like. The result is cached briefly so a busy dashboard does not
re-aggregate the database on every event.

`GET /api/stats/bpm-clusters` buckets every detected tempo into 5-BPM groups, sorted by
count, for the tempo distribution chart.

**Settings → System → Reset statistics** (`POST /api/stats/reset`) stamps a reset
marker rather than deleting rows, so counters restart without losing job history.

## Bulk cleanup

**Settings → System → Remove all jobs** (`POST /api/jobs/remove-all`, 2/minute) deletes
every job row and its on-disk artifacts in one step.

!!! danger "There is no undo"
    Removing all jobs deletes the downloaded media as well. Any share links pointing at
    those jobs stop working immediately.

For automatic cleanup by age, use retention instead — see
[Storage & Retention](../configuration/storage.md).

## Recovery after a restart

Jobs that were `processing`, `downloading`, or `transcoding` when the process stopped
cannot be resumed — the subprocess is gone. At startup they are marked `cancelled` with
the message *"Cancelled because the application restarted during processing"*, so they
end up in a terminal state you can retry from instead of being stuck in flight forever.
