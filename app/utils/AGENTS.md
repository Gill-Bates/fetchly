<!-- Parent: ../AGENTS.md -->

# app/utils/ — Utility Modules

**Purpose:** Reusable helpers for version detection, credentials, file handling, media processing, cookie management, and platform-specific quirks.

Utilities here are imported across the app and provide a single source of truth for operations like watermark rendering, cookie validation, platform detection and public-URL construction.

## Key Files

| File | Purpose |
| --- | --- |
| `version.py` | Reads the version from `pyproject.toml`; exposes it to the UI and `/api/info` |
| `updates.py` | Checks GitHub releases for a newer version; backs `/api/updates` and the Settings → System update tile |
| `changelog.py` | Renders `CHANGELOG.md` for display (markdown → sanitized HTML) |
| `host_stats.py` | Host metrics for the Settings → System tiles: CPU, memory, uptime, storage. Reads `/proc` and the filesystem directly. |
| `watermark.py` | Watermark compositing onto video via FFmpeg |
| `watermark_logo.py` | Validates and stores server-received PNG watermark logos; SVG is rasterized client-side before upload |
| `cookie_import.py` | Parses and normalizes cookies from multiple formats: Netscape files, request headers (cURL/fetch/PowerShell), JSON extensions |
| `cookies.py` | Per-platform cookie file locations and storage under `DATA_DIR` |
| `cookie_status.py` | Reports whether a platform's cookie file exists and is usable |
| `credentials.py` | Admin username normalization and credential checks |
| `duration.py` | Media duration parsing and rounding (`round_seconds`) |
| `fs.py` | Filesystem helpers, notably `get_data_dir()` — the `DATA_DIR` env var, else `./data` |
| `platform.py` | Platform quirks: YouTube, Instagram, TikTok, Facebook (format filters, cookie requirements) |
| `youtube.py` | YouTube-specific handling |
| `public_url.py` | `normalize_public_hostname()` and `build_public_base_url(request, public_hostname)` for share links |
| `template_filters.py` | Custom Jinja2 filters registered in `main.py` |
| `assets.py` | `asset_url()` (the Jinja global that content-hashes `/static` URLs) and `VersionedStaticFiles` (the matching `Cache-Control` policy) |
| `hidden_captcha.py` | Invisible anti-bot protection: honeypot field and signed token |
| `banner.py` | Startup ASCII banner |
| `housekeeping.py` | Filesystem side of the retention sweep: expired job directories, orphaned directories, thumbnail cache. The matching **database** purge is `db.purge_old_jobs()`, called from `main.py::_run_housekeeping_once` — this module never touches SQLite |
| `__init__.py` | Package init |

## For AI Agents

### Adding a New Utility
1. Create the module here
2. Implement functions with docstrings and explicit type hints (ruff enforces a broad rule set; see `[tool.ruff.lint]` in `pyproject.toml`)
3. Import it where needed
4. Add a test in `tests/` — note the naming: tests are named after the subject (`test_duration.py`, `test_public_url.py`, `test_host_stats.py`), **not** `test_utils_*.py`
5. Document it here if it is widely used

### Working with External Services
- **yt-dlp / FFmpeg:** subprocesses. Ruff's `S603`/`S607` are deliberately ignored because binaries are resolved from `PATH` with no shell; keep it that way.
- **Lalal.ai:** credentials come from the `settings` table; the HTTP calls live in `app/lalal.py`
- **GitHub API:** `updates.py` for the release check

### Common Patterns
- **Error handling:** raise or return `None` explicitly; no silent fallbacks (ruff's `TRY`/`SIM` rules are on)
- **Paths:** use `pathlib` — `PTH` rules are enabled
- **Timestamps:** timezone-aware UTC; `DTZ` rules reject naive datetimes
- **Subprocesses:** always handle failure and a missing binary
- **Async:** most modules are synchronous; `host_stats.py`, `updates.py`, and `youtube.py` are async or provide async wrappers. Call modules via `asyncio.to_thread()` from async code if they do blocking I/O
- **Configuration:** read from the `settings` table or the documented environment variables

### Public URLs
`public_url.py` builds share URLs from the incoming request combined with the `public_hostname` **database setting**. An empty setting means "use the host of the request that created the link". There is no `FETCHLY_PUBLIC_URL` environment variable.

### Static Assets and Caching
`asset_url('/static/style.css')` renders `/static/style.css?v=<content hash>` — templates never carry a hand-written `?v=` token, and neither do the `import "./config.js"` specifiers inside the ES modules. Two lint rules keep it that way: `tools/eslint.config.mjs` (`no-restricted-syntax`) for the modules and the `templateAssetVersionHandwritten` source contract for the templates.

`VersionedStaticFiles` serves a URL whose `?v=` still matches the file as immutable for a year and everything else as `no-cache`, so a page held in a browser cache can never pin an outdated asset. The font preloads in `base.html` stay unversioned on purpose: they have to match the URL `@font-face` in `style.css` asks for.

### Host Stats
`host_stats.py` reads `/proc` and the filesystem directly. **psutil is not a dependency** — a comment in the module references psutil only to explain the field handling it mirrors. Do not add the import.

### Working with Watermarks
- **Validation:** `watermark_logo.py` checks type, dimensions and size (the client-side counterpart is `static/js/watermark-logo.js`, capped at 2 MiB)
- **Rendering:** `watermark.py` drives FFmpeg
- **Configuration:** the `video_watermark` setting toggles it
- **Upload path:** `POST /api/settings/watermark-logo`

### Working with Cookies
- **Import:** `cookie_import.py` parses Netscape format
- **Storage:** `cookies.py` resolves per-platform paths under `DATA_DIR`
- **Status:** `cookie_status.py` exposes usability to the UI and the worker
- **Platform quirks:** documented in `platform.py`

## Dependencies

### Internal
- Imported by `/app/main.py`, `/app/routes/`, `/app/worker.py`, `/app/analysis_worker.py`, `/app/db.py`

### External
- **FFmpeg:** watermark rendering (subprocess)
- **yt-dlp:** platform handling (subprocess)
- **httpx:** update check and outbound HTTP
- **markdown, nh3:** changelog rendering and sanitizing
- **Jinja2:** filter registration
- **Standard library:** `pathlib`, `hmac`, `secrets`, `subprocess`

No psutil. No argon2 — session tokens are HMAC-SHA256, signed in `app/session.py`.

## Manual

### Testing Utilities
```bash
pytest tests/test_duration.py -v
pytest tests/test_public_url.py -v
pytest tests/test_host_stats.py -v
pytest tests/test_version_source.py -v     # version is read from pyproject.toml
pytest tests/test_watermark.py tests/test_watermark_logo.py -v
pytest tests/test_cookie_import.py tests/test_cookie_files.py tests/test_cookie_status.py -v
```

Tests are unittest-style classes; subclass the helpers in `tests/_support.py` rather than writing pytest fixtures.

### Debugging Watermark Issues
1. `ffmpeg -version` — confirm the binary is on `PATH`
2. Confirm the stored logo exists under `$DATA_DIR`
3. `pytest tests/test_watermark.py -v -s`
4. Read the FFmpeg stderr captured by the subprocess call

### Debugging Cookie Issues
1. Confirm the file starts with `# Netscape HTTP Cookie File`
2. `pytest tests/test_cookie_import.py tests/test_cookie_status.py -v`
3. Check the per-platform path resolution in `cookies.py` against `$DATA_DIR`

### Version and Update Flow
1. `version.py` reads the version from `pyproject.toml` — the single source of truth
2. `updates.py` queries GitHub releases and compares
3. `/api/updates` serves the result to the Settings → System tile
4. `tests/test_version_source.py` guards the arrangement, including rejecting `==` pins that creep into runtime dependencies

---

**Last Updated:** 2026-09-20  
**Language:** Python 3.13+  
**Key Dependencies:** yt-dlp, FFmpeg, httpx, markdown/nh3  
**Design:** Stateless helpers; configuration comes from the settings table or the environment
