# Development Setup

Setting up fetchly for local development, outside the container.

## Requirements

| Requirement | Notes |
|---|---|
| Linux | Host-stats and process handling are Linux-first |
| Python 3.13 | `python3.13-venv`, `python3.13-dev` |
| `ffmpeg` | On `PATH` |
| `git` | For version metadata |
| `yt-dlp` + `yt-dlp-ejs` | Installed via pip, not pinned in `requirements.txt` |
| `deno` | On `PATH` — solves YouTube JS challenges |

Essentia and `beat_this` (for [BPM analysis](../features/bpm.md)) are optional in a
dev environment; the app runs without them and simply skips analysis.

## Clone and install

```bash
git clone https://github.com/Gill-Bates/fetchly.git
cd fetchly

python3.13 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
pip install yt-dlp yt-dlp-ejs
```

`requirements.txt` pins everything the app imports directly, plus a
`--extra-index-url` for the CPU build of PyTorch (`beat_this`'s dependency). yt-dlp and
`yt-dlp-ejs` are intentionally installed separately, outside `requirements.txt`, since
yt-dlp updates on its own cadence as platforms change.

## Run it

```bash
export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
export LOG_LEVEL=debug
export UVICORN_RELOAD=1
export UVICORN_RELOAD_EXCLUDES="data,.git"

python run.py
```

`UVICORN_RELOAD=1` restarts on code changes. Exclude `data/` and `.git/` from the
watch — the database and job files change constantly and would otherwise trigger
reload loops.

Data lands in `data/` relative to the working directory unless `DATA_DIR` says
otherwise.

## Running the tests

```bash
pip install pytest pytest-asyncio pytest-cov
pytest
```

Tests live in `tests/` — one file per module under test, largely mirroring `app/`.
Some exercise real subprocess calls (ffmpeg) and are naturally slower.

### JavaScript tests

```bash
cd tools/ui-lint   # or wherever the JS toolchain is configured
npm install
npm test
```

`tests/js/*.test.mjs` covers front-end contracts — the CSRF token helper, the
confirmation modal, the cookie-paste dialog, safe-redirect handling — as plain Node
tests, no browser required.

## Code style

- Python: type-annotated, `from __future__ import annotations` where the codebase uses
  it, docstrings that explain *why* a design choice was made, not just *what* the code
  does
- No native browser dialogs (`confirm`/`alert`/`prompt`) — use the shared
  `confirmModal()` in `app/static/js/confirm.js`
- New settings keys go through the allow-list in `app/db.py`
  (`_SETTINGS_DEFAULTS`, `_SETTINGS_TYPES`), never accepted unchecked

## Building the Docker image locally

```bash
DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile -t fetchly:dev .
```

The Dockerfile is multi-stage; expect a longer build on the first run while the
`ffmpeg` and `essentia` stages compile.

## Project layout

```text
app/
├── main.py              # FastAPI app, lifespan, background tasks
├── routes/               # One module per route family
├── worker.py             # Download/transcode worker threads
├── analysis_worker.py    # BPM analysis process pool
├── bpm*.py                # Tempo detection cascade
├── governor.py           # Resource sizing and semaphores
├── db.py                 # SQLite schema, settings, queries
├── session.py             # Session cookie signing and validation
├── lalal*.py               # Lalal.ai client and product rules
├── utils/                # Focused single-purpose helpers
├── templates/             # Jinja2 templates
└── static/                # CSS, JS, vendored assets
middleware/
└── csrf.py                # Double-submit CSRF middleware
tests/
├── test_*.py               # pytest, one file per module under test
└── js/*.test.mjs            # front-end contract tests
```

See [Architecture](architecture.md) for how these pieces fit together.

## Where to next

<div class="grid cards" markdown>

-   :material-sitemap:{ .lg .middle } **How it fits together**

    ---

    [:octicons-arrow-right-24: Architecture](architecture.md)

-   :material-source-pull:{ .lg .middle } **Submitting changes**

    ---

    [:octicons-arrow-right-24: Contributing](contributing.md)

</div>
