<!-- Parent: ../AGENTS.md -->

# app/runtime/ — Process Runtime

**Purpose:** Resource governance and server logging that shape how the single process runs.

## Key Files

| File | Purpose |
| --- | --- |
| `governor.py` | Resource governor: cgroup/cpuset CPU detection, memory thresholds, semaphores, backpressure |
| `gunicorn_logging.py` | Gunicorn logger class `FetchlyGunicornLogger`; referenced by string in `docker/entrypoint.sh` (`--logger-class app.runtime.gunicorn_logging.FetchlyGunicornLogger`) and checked by `tests/test_gunicorn_logging.py` |

## For AI Agents
- Moving `gunicorn_logging.py` requires changing the entrypoint flag and the test together
- Tests: `tests/test_governor_semaphores.py`, `tests/test_memory_backpressure.py`, `tests/test_runtime_settings.py`
