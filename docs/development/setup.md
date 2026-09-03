# Development Setup

Setting up fetchly for local development, outside the container.

## Requirements

| Requirement | Notes |
|---|---|
| Linux | Host-stats and process handling are Linux-first |
| Python 3.13 | `python3.13-venv`, `python3.13-dev` |
| `ffmpeg` | On `PATH` |
| `git` | For version metadata |
| `yt-dlp` + `yt-dlp-ejs` | Installed via pip, deliberately not pinned in `pyproject.toml` |
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
pip install --extra-index-url https://download.pytorch.org/whl/cpu -e ".[dev]"
pip install yt-dlp yt-dlp-ejs
```

`pyproject.toml` is the single manifest: it holds the release version, the runtime
dependencies, and the `dev` and `docs` extras. It replaced `VERSION`,
`requirements.txt` and `docs/requirements-docs.txt` — a change to any of those three
now happens in one file, and `app/utils/version.py` reads `[project] version` straight
back out at runtime.

`--extra-index-url` picks the CPU build of PyTorch (`beat_this`'s dependency); without
it pip resolves the CUDA wheels and pulls in gigabytes of GPU code the app never runs.
yt-dlp and `yt-dlp-ejs` stay outside the manifest on purpose, since yt-dlp updates on
its own cadence as platforms change.

Need a flat requirements file — for a tool that only speaks `-r`, or for a scan?
Generate it instead of keeping a second copy:

```bash
python tools/pyproject-deps.py > requirements.txt          # runtime
python tools/pyproject-deps.py docs > requirements-docs.txt
python tools/pyproject-deps.py dev                          # pytest + ruff
```

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
pytest
```

`pytest` picks up its configuration from `[tool.pytest.ini_options]` in
`pyproject.toml`. Tests live in `tests/` — one file per module under test, largely
mirroring `app/`. Some exercise real subprocess calls (ffmpeg) and are naturally
slower.

### JavaScript tests

From the repository root:

```bash
npm install
npm test
```

`tests/js/*.test.mjs` covers front-end contracts — the CSRF token helper, the
confirmation modal, the cookie-paste dialog, safe-redirect handling — as plain Node
tests, no browser required.

Note the glob: `node --test tests/js/` (the directory form) needs Node 24, so the
`npm test` script spells out `node --test "tests/js/*.test.mjs"` instead.

## Linting

Three linters:

| Target | Config | Run with |
|---|---|---|
| Python | `[tool.ruff]` in `pyproject.toml` | `ruff check .` |
| Front-end JS | `tools/eslint.config.mjs` | `npm run lint:js` |
| CSS | `tools/stylelint.config.mjs` | `npm run lint:css` |

`npm run lint` runs both front-end linters; `npm run lint:fix` applies what is safely
fixable. All three run on every push and pull request via
`.github/workflows/ci.yml`.

A few rules encode project decisions rather than style:

- **No native browser dialogs.** ESLint's `no-alert` and `no-restricted-globals` fail
  the build on `confirm()`/`alert()`/`prompt()`. Use `confirmModal()` from
  `app/static/js/confirm.js`. The single sanctioned exception is inside `confirm.js`
  itself, on the branch where Bootstrap failed to load.
- **No `100vh` without a `100dvh` companion.** Stylelint warns, because Safari's
  collapsing toolbar makes `100vh` overflow on iPhone and in iPad Split View.
- **`-webkit-*` prefixes are kept.** `property-no-vendor-prefix` is off: those
  prefixes are load-bearing on iOS and iPadOS, not legacy.

## Browser audit (ui-lint)

`tools/ui-lint` drives the running app through Playwright and reports layout,
accessibility, contrast and iOS/iPadOS problems. It needs a live server, so it is a
local pre-release check rather than part of the PR gate:

```bash
npm run ui-lint:install   # once: installs Playwright + Chromium and WebKit
python run.py &           # the app must be reachable
npm run ui-lint
```

It audits five device profiles, chosen around the breakpoint in
`app/static/style.css` where the desktop jobs table gives way to the mobile feed
(`@media (max-width: 1024px)`):

| Profile | Device | Width | Engine | Layout |
|---|---|---|---|---|
| `desktop` | — | 1440px | Chromium | desktop table |
| `mobile` | iPhone 13 | 390px | WebKit | feed |
| `tablet` | iPad Mini | 768px | WebKit | feed |
| `tablet-landscape` | iPad Mini landscape | 1024px | WebKit | feed (breakpoint edge) |
| `tablet-wide` | iPad Pro 11 landscape | 1194px | WebKit | desktop table, touch input |

Form factor and touch are separate axes in the runner, which is what `tablet-wide`
exists to prove: it renders the desktop table *and* needs 44px hit areas. Audits are
gated on the axis they actually depend on — `isMobile` for which DOM renders, `isTouch`
for hit areas and Safari's viewport quirks, `isPhone` for the pixel contracts written
against 390px.

Results land in `results.json` under the output directory the run prints, and the
process exits non-zero when any hard failure is found.

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
