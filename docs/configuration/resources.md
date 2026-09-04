# Resources & Workers

fetchly runs downloads, transcodes, and audio analysis concurrently inside a single
process, bounded by a resource governor that sizes itself to the host.

## Why a single process

`WORKERS` must stay at `1`. The job queue and the SSE subscriber registry live in
process memory with no cross-process coordination:

- A second Gunicorn worker would process the same job twice
- Clients would subscribe for events to a process that never sees their job

This is a correctness constraint, not a throughput trade-off, and the container
entrypoint enforces it. Parallelism comes from worker threads and semaphores inside the
one process.

## CPU detection

The governor detects the **effective** CPU allocation, not just `nproc`. It is
cgroup-aware, so a container limited to `--cpus=1.5` is sized for 1.5 CPUs rather than
for the host's core count.

## Auto-sizing

With every variable left at its default:

| Limit | Formula | Bounds |
|---|---|---|
| Worker threads | `ceil(cpus × 2)` | 1–8 |
| Queue depth | `workers × 2` | — |
| CPU semaphore | `ceil(cpus)` | ≥ 1 |
| Analysis semaphore | `min(2, ceil(cpus))` | ≥ 1 |
| I/O semaphore | `ceil(cpus × 4)` | ≥ 2 |
| Transcode semaphore | `min(2, ceil(cpus))` | ≥ 1 |
| Parallel fragments | `ceil(cpus × 1.5)`, capped by `free MB ÷ 64` | 2–8 |

The shapes reflect the workloads: downloads are I/O-bound and get a generous limit,
while transcoding and BPM analysis are CPU- and memory-hungry and are held to at most
two at a time.

### Worked example

| Effective CPUs | Workers | Queue | CPU | Analysis | I/O | Transcode |
|---:|---:|---:|---:|---:|---:|---:|
| 0.5 | 1 | 2 | 1 | 1 | 2 | 1 |
| 1 | 2 | 4 | 1 | 1 | 4 | 1 |
| 2 | 4 | 8 | 2 | 2 | 8 | 2 |
| 4 | 8 | 16 | 4 | 2 | 16 | 2 |
| 8 | 8 | 16 | 8 | 2 | 32 | 2 |

Parallel fragments are the one limit re-evaluated per download rather than at startup:
with **Parallel fragments per download** on `Automatic`, a host that is currently short
on free memory gets fewer fragments than the CPU quota alone would allow (2 on 128 MB
free, 4 on 256 MB, 8 from 512 MB up). A fixed value of `1`–`16` skips the probe.

## Runtime settings and operator overrides

**Settings → Processing → Downloads** controls the parallel fragments per download, where
`Automatic` is the default. **Settings → General → Runtime limits** controls download
worker count, download and transcode timeouts, and input size. A worker count of `0` means automatic sizing and
applies after the next restart. The BPM-analysis limits and the Lalal result limit
live in the Lalal.ai tile under **Settings → Integrations**.

The remaining resource-governor limits are operator-level overrides. `0` means "auto".

| Variable | Effect |
|---|---|
| `WORKER_QUEUE_MAXSIZE` | Queue depth before submissions are rejected |
| `CPU_SEMAPHORE_LIMIT` | Concurrent CPU-bound operations |
| `ANALYSIS_SEMAPHORE_LIMIT` | Concurrent BPM analyses |
| `IO_SEMAPHORE_LIMIT` | Concurrent I/O-bound operations |
| `TRANSCODE_SEMAPHORE_LIMIT` | Concurrent ffmpeg transcodes |

```yaml
environment:
  TRANSCODE_SEMAPHORE_LIMIT: 1   # a small VPS that must stay responsive
```

## Backpressure

| Variable | Default | Description |
|---|---|---|
| `ENABLE_BACKPRESSURE` | on | Reject new jobs when resources are short |
| `MEMORY_THRESHOLD_MB` | `256` | Stop accepting jobs below this much available memory |

With backpressure on, the queue is bounded and a submission that arrives with a full
queue is rejected with `503 Service Unavailable` rather than being accepted into a
backlog that will never drain. Turning it off makes the queue unbounded — which trades
a clear error for an eventual out-of-memory kill.

## Timeouts

Download and transcode timeouts are configured in **Settings → General → Runtime
limits**; the BPM-analysis timeout lives in **Settings → Integrations → Lalal.ai**.
Container lifecycle timeouts remain deployment settings:

| Variable | Default | Applies to |
|---|---|---|
| `TIMEOUT` | 60 s | Gunicorn worker (container) |
| `GRACEFUL_TIMEOUT` | 15 s | Shutdown before SIGKILL (container) |

A subprocess that exceeds its timeout is terminated and the job is marked `error` with a
message you can act on.

## Sizing guidance

=== "Small VPS (1 CPU, 1 GB)"

    ```yaml
    environment:
      TRANSCODE_SEMAPHORE_LIMIT: 1
      ANALYSIS_SEMAPHORE_LIMIT: 1
      MEMORY_THRESHOLD_MB: 192
    ```

    Set **Download workers** to `2` in Runtime limits. BPM analysis is the heaviest
    step; if memory is tight, keep the analysis semaphore at 1 or leave analysis to a
    bigger host.

=== "Home server (4 CPU, 8 GB)"

    Leave everything at auto. The defaults land on 8 workers, a 16-deep queue, and 2
    concurrent transcodes.

=== "Download-heavy, transcode-light"

    ```yaml
    environment:
      IO_SEMAPHORE_LIMIT: 32
      TRANSCODE_SEMAPHORE_LIMIT: 2
    ```

    Set **Download workers** to `8` in Runtime limits.

## Observing

**Settings → System → Host resources** shows storage, CPU, RAM, and uptime, sampled
from `/proc`. Metrics that are unavailable degrade to empty rather than failing the
panel.

```bash
curl -s http://127.0.0.1:8000/api/system/host    # requires a session
docker stats fetchly
```

!!! tip "Reading a full queue"
    Repeated `503`s on submit mean the queue is saturated, not that something is
    broken. Either raise **Download workers** in Runtime limits (if the host has
    headroom) or accept that the host is at capacity — raising
    `WORKER_QUEUE_MAXSIZE` alone only hides the wait.
