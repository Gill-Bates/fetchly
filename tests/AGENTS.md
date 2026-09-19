<!-- Parent: ../AGENTS.md -->

# tests/ — Test Suites

**Purpose:** Backend tests (pytest running unittest-style classes) and frontend tests (`node:test`), covering auth, cookies, BPM, watermarking, database behavior, API contracts, worker hardening, UI contracts and accessibility.

## Key Files

| File | Purpose |
| --- | --- |
| `conftest.py` | Imports `tests._support` for its side effect: `FETCHLY_SECRET_KEY` is set before pytest imports any test module. **Defines no fixtures.** |
| `_support.py` | Shared setup: the secret-key bootstrap plus `IsolatedDbTestCase` and `WebAppTestCase` base classes |
| `test_auth_credentials.py`, `test_auth_flow.py` | Admin credential handling, login/logout, session behavior |
| `test_cookie_files.py`, `test_cookie_import.py`, `test_cookie_routes.py`, `test_cookie_status.py` | Netscape parsing, per-platform storage, routes, usability reporting |
| `test_bpm_beat_this.py`, `test_bpm_naming.py`, `test_bpm_normalization.py` | Tempo detection, the `_94bpm` tag folded into download filenames, octave/half-time normalization |
| `test_watermark.py`, `test_watermark_logo.py` | FFmpeg compositing and uploaded-logo validation |
| `test_compatible_output.py` | API/format response-shape contract |
| `test_db_job_statuses.py` | Job status transitions and the valid status set |
| `test_worker_hardening.py` | Worker resilience: failure handling, cancellation, recovery |
| `test_runtime_settings.py`, `test_settings_migration.py` | Settings persistence and schema/setting migration |
| `test_host_stats.py` | Host metrics parsing |
| `test_changelog.py`, `test_version_source.py` | Changelog rendering; version source and the no-`==`-pins rule |
| `test_duration.py`, `test_job_duration.py` | Duration parsing and job duration reporting |
| `test_lalal_minutes.py`, `test_lalal_policy.py`, `test_lalal_route_safety.py` | Lalal.ai quota, policy guards, route safety |
| `test_public_url.py` | Share-link host normalization and base-URL construction |
| `test_remove_all_jobs.py` | Bulk job deletion |
| `test_csp_wavesurfer.py` | CSP compatibility with the vendored WaveSurfer bundle |
| `test_template_filters.py` | Custom Jinja2 filters |
| `test_mobile_settings_layout.py` | Settings page layout on a mobile viewport |
| `test_status_mapping_parity.py` | Backend status vocabulary vs. the frontend mirror in `static/js/config.js` |
| `test_gunicorn_logging.py` | Custom Gunicorn logger |
| `test_stat_tile_captions.py` | Short captions on the Settings → System stat tiles |
| `test_assets.py` | Content-hash cache busting for `/static` assets: the `?v=` token is derived from the file, the URL it decorates resolves, and the token changes when the file does |
| `test_share_links.py` | Share-token entropy (≥128 bits), per-job distinctness, and what the redeem regex accepts — including shorter legacy tokens |
| `test_rate_limit_client_ip.py` | Client-IP resolution behind a trusted proxy: forwarded chains, bracketed IPv6, and malformed entries falling back to the socket peer |
| `test_audio_source_header.py` | `X-Audio-Quality` on `/audio-source/{job_id}` reports storage provenance, not codec quality |
| `test_analysis_worker.py` | Analysis submission: what a completed analysis writes back, and staying responsive while waiting on the child process |
| `test_governor_semaphores.py` | `SharedSemaphore`: one budget per workload across worker threads and the event loop, cancellation safety, and the `*_sync` accessors returning the same object |
| `test_worker_cancel_retry_race.py` | The worker's terminal writebacks are conditional, so a stale cancel cannot land on a row a retry already moved |

39 Python test files in total.

## Subdirectories

| Directory | Purpose |
| --- | --- |
| `js/` | 17 `node:test` suites (`*.test.mjs`): UI contracts, accessibility, layout stability, device profiles, console severity, network findings, audit result summaries, element-id contracts, toast/modal/CSRF/redirect safety |
| `js/helpers/` | `fake-dom.mjs` — the shared fake DOM |

## For AI Agents

### Working on Backend Tests
These are **unittest-style classes executed by pytest**. There are no pytest fixtures anywhere in the suite — no `db_session`, no `client`, no `@pytest.fixture` in `conftest.py`.

- **New test file:** `test_<subject>.py` in `tests/`
- **Needs an isolated database:** subclass `IsolatedDbTestCase` from `tests/_support.py`
- **Needs a FastAPI TestClient:** subclass `WebAppTestCase`
- **Needs only the secret key:** nothing extra — `conftest.py` has already set it before your module is imported
- **Import order matters:** `tests/_support.py` must be imported before any `app.*` import, because `app/session.py` and `app/main.py` require `FETCHLY_SECRET_KEY` at import time. Running under pytest this is automatic via `conftest.py`.
- **Mocking external services:** `unittest.mock.patch` for yt-dlp, FFmpeg and Lalal.ai calls
- **Run:** `pytest`, or `pytest tests/test_auth_flow.py -v`

```python
from tests._support import WebAppTestCase


class SubmitTest(WebAppTestCase):
    def test_rejects_missing_url(self):
        response = self.client.post("/api/submit", json={})
        self.assertEqual(response.status_code, 422)
```

- **Per-file ruff relaxations:** `tests/*` may use `assert`, fixed credentials, fixed temp paths and `print()` — see `[tool.ruff.lint.per-file-ignores]`
- **Markers:** `@pytest.mark.slow` marks tests that shell out to real ffmpeg/essentia
- **Strict config:** `addopts = "-q --strict-markers --strict-config"`, so an unregistered marker is an error
- **Warnings are errors:** `DeprecationWarning` raised from `app.*` or `middleware.*` fails the run

### Working on JavaScript Tests
- **New test file:** `feature-name.test.mjs` in `tests/js/`
- **Framework:** `node:test` with `node:assert/strict` — not vitest, not jest, not @testing-library

```javascript
import assert from "node:assert/strict";
import test from "node:test";

import { installFakeDom } from "./helpers/fake-dom.mjs";

const { body } = installFakeDom({ withBootstrap: false });

test("renders the empty state", () => {
    assert.equal(body.querySelector("[data-empty]"), null);
});
```

- **Run:** `npm test` → `node --test "tests/js/*.test.mjs"`. The glob is baked into the script, so appended CLI arguments do not filter it. To run one file: `node --test tests/js/toast.test.mjs`. To filter by name: `node --test --test-name-pattern "toast" tests/js/*.test.mjs`.
- **Accessibility and layout** are checked by the `ui-lint-*.test.mjs` suites here, and separately by the Playwright audit in `tools/ui-lint/` (`npm run ui-lint`)

### Testing Strategy
- **Unit:** single function or module behavior
- **Integration:** workflows through `WebAppTestCase`
- **Contract:** response shapes, and backend/frontend status parity (`test_status_mapping_parity.py`, `config-contract.test.mjs`)
- **Regression:** add a test for every bug fix

### Common Patterns
- **Assertions:** `self.assertEqual(...)` in Python, `assert.equal(...)` in JavaScript
- **Job statuses in assertions:** use real values — `done`, `analysis_done`, `error`, `cancelled`, `queued`, `downloading`, `processing`, `transcoding`, `analysis`. There is no `completed` or `failed`.
- **Temp files:** `tempfile` / `tmp_path`; `IsolatedDbTestCase` handles database isolation including the `db._database_path_prepared` memoization gotcha
- **Naming:** `test_<feature>_<scenario>`

## Dependencies

### Internal
- `/app/` and `/middleware/` — the code under test
- `tests/_support.py` — the only shared setup layer

### External
- **Python:** pytest, pytest-asyncio, pytest-cov (all pinned in `[project.optional-dependencies] dev`), `unittest.mock`
- **JavaScript:** Node.js built-ins only — `node:test`, `node:assert/strict`. The `tests/js/` suites have no npm dependencies of their own; axe-core and Playwright belong to `tools/ui-lint/`.

## Manual

### Running Tests
```bash
pytest                         # all Python tests
npm test                       # all JavaScript tests
pytest && npm test             # both, before committing
```

### Running Specific Tests
```bash
pytest tests/test_auth_flow.py
pytest tests/test_bpm_naming.py -v
pytest -m "not slow"                       # skip real-subprocess tests
node --test tests/js/toast.test.mjs
```

### Coverage
```bash
pytest --cov --cov-report=html
# Config lives in [tool.coverage.*] in pyproject.toml; source = app, middleware.
```

There is no enforced coverage threshold — neither `fail_under` in `pyproject.toml` nor `--cov-fail-under` in CI.

### Debugging Failed Tests
```bash
pytest tests/test_worker_hardening.py -v -s
pytest tests/test_worker_hardening.py --pdb
```

### CI Integration
`.github/workflows/ci.yml` runs, as separate jobs: `ruff check --output-format=github .`, `pytest -q`, and `npm run lint:js` / `lint:css` / `lint:contracts` / `npm test`. `.github/workflows/ui-audit.yml` runs the Playwright audit. All must pass.

---

**Last Updated:** 2026-09-19  
**Python:** pytest runner, unittest-style classes, no fixtures  
**JavaScript:** `node:test` + `node:assert/strict`  
**Counts:** 39 Python files, 17 JavaScript files
