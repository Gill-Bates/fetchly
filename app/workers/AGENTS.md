<!-- Parent: ../AGENTS.md -->

# app/workers/ — Job Workers

**Purpose:** The in-process download/transcode job processor and the audio-analysis submission layer.

## Key Files

| File | Purpose |
| --- | --- |
| `worker.py` | Consumes an in-process `queue.Queue`, runs yt-dlp/FFmpeg as subprocesses, parses progress, manages the status lifecycle |
| `analysis_worker.py` | Submission layer for audio analysis; runs `audio.analysis` in a `spawn` child process and writes results back to the database |

## For AI Agents
- `worker.py` imports `db`, `runtime.governor`, `workers.analysis_worker` and `utils`, but **not** `routes`; routes import `worker.py` (`submit_download`, `cancel_job`, `sanitize_filename`)
- The analysis child is started with `multiprocessing.get_context("spawn")` and `_analysis_child` is pickled by its qualified name (`app.workers.analysis_worker`); renaming or moving the module changes that name
- Tests: `tests/test_worker_hardening.py`, `tests/test_worker_cancel_retry_race.py`, `tests/test_analysis_worker.py`
