<!-- Parent: ../AGENTS.md -->

# docker/ — Container Runtime Configuration

**Purpose:** Dockerfile, compose template, entrypoint script and APT preferences for building and running fetchly in containers.

## Key Files

| File | Purpose |
| --- | --- |
| `Dockerfile` | Four-stage build: `ffmpeg`, `essentia`, `builder`, `runtime`. All stages share `ARG PYTHON_BASE=python:3.13-slim`. |
| `entrypoint.sh` | Startup: resolves `TORCH_HOME`, enforces `WORKERS=1`, creates and chowns required directories, drops to an unprivileged user with `gosu`, then `exec`s Gunicorn |
| `docker-compose.yml` | Example service: image, port mapping, environment, volume, log rotation, `no-new-privileges` |
| `README_Docker.md` | Docker-facing documentation |
| `apt-no-distro-media.pref` | APT preferences, copied to `/etc/apt/preferences.d/no-distro-media` in the runtime stage |

## For AI Agents

### Working on the Docker Build
- **Build context is the repo root, and the Dockerfile is not there:**
  ```bash
  docker build -f docker/Dockerfile -t fetchly:local .
  ```
- **Stages:**
  - `ffmpeg` — fetches a static FFmpeg/FFprobe build from the BtbN FFmpeg-Builds release named by `FFMPEG_RELEASE_URL`. FFmpeg does **not** come from `apt-get`.
  - `essentia` — compiles essentia from source (`ESSENTIA_REPO`, `ESSENTIA_REF`) because upstream publishes no `linux/aarch64` wheel at all
  - `builder` — builds the virtualenv, installs yt-dlp / yt-dlp-ejs / Deno at the versions passed in, and fetches WaveSurfer (unpinned `@latest`) — see "Fetching WaveSurfer" below.
  - `runtime` — installs the few runtime apt packages, copies `ffmpeg`/`ffprobe`, the `/venv`, `deno` and the application source
- **Adding a Python dependency:** add it to `pyproject.toml`. The builder reads `[project].dependencies` out of it with `tomllib` and installs that set without installing the project, so the layer stays cached across source-only changes. (`tools/pyproject-deps.py` is the release workflow's copy of the same step, not the builder's.)
- **torch and torchaudio** are the only packages installed from `TORCH_CPU_INDEX`, with `--index-url` and `--no-deps` so that index is never merged into PyPI's namespace for the rest of the manifest — it is not a torch-only host, it also serves `jinja2`, `numpy`, `sympy`, `filelock`, `fsspec` and `setuptools`. Their own dependencies come from PyPI with everything else, and the stage asserts `torch.version.cuda is None` afterwards. `pyproject.toml` expresses the same split for resolvers: `[[tool.uv.index]] pytorch-cpu` is `explicit = true` and `[tool.uv.sources]` binds those two names to it, which is why `torchaudio` is listed in `[project.dependencies]` although nothing imports it — a source binding does not reach a purely transitive dependency.
- **Adding a system package:** extend the `apt-get install` line in the `runtime` stage
- **`APT_CACHE_DATE`** is declared globally *and* redeclared inside `ffmpeg`, `builder` and `runtime`. A global `ARG` is invisible to a stage that does not redeclare it, so a new apt layer must carry its own `ARG APT_CACHE_DATE` line or the value passed with `--build-arg` reaches nothing and `apt-get upgrade` becomes a permanent cache hit. The `essentia` stage deliberately stays out: nothing it installs ships, and keying it on a daily value would recompile essentia and its static 3rd-party chain on every release. The release workflow passes one date for both architectures.
- **`apt-get update`, `apt-get upgrade -y`, install and cleanup stay in one `RUN`** per stage — splitting them reintroduces the stale-index problem the cache buster exists to avoid.
- **yt-dlp:** deliberately unpinned and absent from `pyproject.toml`; the version is supplied by `ARG YTDLP_VERSION` at build time as a **bare version** (`2026.1.1`), not a specifier — the Dockerfile builds the `==` itself and rejects a value that already carries one

### Fetching WaveSurfer

**Fetched unpinned at build time ("latest is greatest").**
The `builder` stage `ADD`s `https://unpkg.com/wavesurfer.js@latest/package.json`
to resolve the current version, then `curl`s that version's
`dist/wavesurfer.esm.js` and `dist/plugins/regions.esm.js` into `/wavesurfer/`
and writes the resolved version to `/wavesurfer/version`. The `runtime` stage
overlays the fetched `dist/` onto
`app/static/vendor/wavesurfer/dist/` (the path `trim.js` imports, unchanged)
with `COPY --from=builder`, and copies `/wavesurfer/version` to
`/app/.wavesurfer_version`. `entrypoint.sh` reads that file at container start
and exports `WAVESURFER_VERSION` for `app/utils/version.py`'s
`get_wavesurfer_version()`, so the Settings → System tile reports the version
the build actually fetched.

- **The committed bundle is a local-dev fallback, not what ships.**
  `app/static/vendor/wavesurfer/dist/{wavesurfer.esm.js,plugins/regions.esm.js}`
  stay committed (force-added past `.gitignore`, which still lists the
  directory) so `python run.py` and any non-Docker checkout have a working
  bundle. In the image the build-time overlay replaces them, so the committed
  version does not decide what the container serves. `docker/wavesurfer.version`
  is the matching fallback the local `get_wavesurfer_version()` reads when no
  `WAVESURFER_VERSION` env is set.
- **Accepted security trade-off.** The fetched bundle is same-origin
  JavaScript served under `script-src 'self'`, pulled over TLS from unpkg with
  no checksum. A compromised or tampered upstream `@latest` release would be a
  full XSS in the admin context. This was deliberately re-enabled over the
  previous git-vendored, pinned design: the project wants upstream fixes to
  reach a plain rebuild without a manual bump, and accepts the reintroduced
  trust surface. essentia stays pinned (`ESSENTIA_REF`); WaveSurfer does not.
- **CSP uses a per-request nonce, not a fixed hash.** Because the fetched
  version — and the shadow-DOM stylesheet it injects, which interpolates the
  waveform height — changes over time, a hard-coded `style-src` hash would
  drift and block the trim view's stylesheet. `app/main.py::build_csp()` emits
  `style-src 'self' 'nonce-<per-request>'`; `base.html` exposes the same nonce
  as `data-wavesurfer-csp-nonce`, and `trim.js` passes it to
  `WaveSurfer.create({ cspNonce })`, which stamps it onto the injected
  `<style>`. This admits exactly that one sheet without `'unsafe-inline'` and
  tracks any version automatically. `tests/test_csp_wavesurfer.py` guards the
  nonce directive, that it is fresh per response, and that the rendered page
  and the header carry the same value.
- **Still not covered by the Trivy scan** in `.github/workflows/docker-build.yml`
  (`vuln-type: os,library` needs a `package.json` or lockfile to detect a Node
  library; the fetched bundle ships neither). Watch upstream advisories by hand.
- **Pinning a version (if ever needed):** pass `--build-arg WAVESURFER_VERSION=7.12.6`
  to hold a specific release; the default is `latest`. The `regions` plugin API
  used in `app/static/js/trim.js` is the thing to re-verify across a major bump.

### Working on the Entrypoint
- **`WORKERS` is fixed at 1 and enforced, not merely defaulted.** The job queue *and* the SSE subscriber registry live in process memory with no cross-process coordination. A second Gunicorn worker means the same job processed twice and clients subscribed to a process that never sees their job's events — a correctness failure, not a throughput trade-off. CPU parallelism comes from the governor's semaphores instead.
- **`DATA_DIR` and `TORCH_HOME` are validated before anything privileged runs.** Both reach a root `chown`/`chmod` (`DATA_DIR` a recursive one), so `validate_managed_dir` rejects relative paths, protected system paths and their descendants, including paths reached through symlinks. `/var/lib/fetchly` is an explicit custom-data exception. The `jobs` and `cookies` paths are checked separately. `REQUIRED_DIRS` holds `${DATA_DIR}`, `${DATA_DIR}/jobs` (the parent of every per-job `DATA_DIR/jobs/<uuid>` directory), `${DATA_DIR}/cookies` and `TORCH_HOME` itself and **not** its parent: `mkdir -p` creates the parent anyway, torch only writes below `TORCH_HOME`, and an entry in that array is chown'ed and chmod'ed — `TORCH_HOME=/etc/torch` used to hand `/etc` to `appuser` with mode 700. Do not add a parent directory to that array.
- **Startup sequence:** resolve `TORCH_HOME` (stored under `DATA_DIR` so the ~81 MB beat-this checkpoint survives a container recreate) and `WAVESURFER_VERSION` (read from `/app/.wavesurfer_version`, written by the `builder` stage from the fetched version and copied in by the `runtime` stage — see "Fetching WaveSurfer" above) → validate managed paths and the non-root `APP_USER` → create and chown required directories → `exec gosu "$APP_USER"` → `exec gunicorn app.main:app --workers "$WORKERS" --worker-class uvicorn_worker.UvicornWorker --logger-class app.runtime.gunicorn_logging.FetchlyGunicornLogger`
- **No admin account is created** at startup, and **no separate worker process is spawned** — the worker runs inside the app process
- **Database setup** happens in the FastAPI lifespan hook, not in the entrypoint
- **Graceful shutdown:** `GRACEFUL_TIMEOUT` bounds how long Gunicorn waits for the lifespan shutdown (stop workers, WAL checkpoint, close SQLite). It must stay below the orchestrator's kill grace period — compose sets `stop_grace_period: 20s` — so the checkpoint actually completes.
- **Logging:** stdout/stderr

### Working on Docker Compose
- **Volume:** `./data:/app/data` — database, per-job directories under `jobs/`, cookie jars, caches
- **Required:** `FETCHLY_SECRET_KEY`; compose fails fast with `:?required` if it is unset
- **Variables it reads:** `FETCHLY_TAG`, `FETCHLY_BIND`, `FETCHLY_PORT`, `FETCHLY_SECRET_KEY`, `LOG_LEVEL`, `TIMEOUT`, `FORWARDED_ALLOW_IPS` (default `127.0.0.1,::1`; name the proxy's address when it is not on loopback). `TZ` is pinned to `Etc/UTC`: the app works in UTC and the browser localizes
- **Port publishing:** `"${FETCHLY_BIND:-127.0.0.1}:${FETCHLY_PORT:-8000}:8000"`. The host address is not optional decoration — without it Docker publishes on every interface, and a fresh install has no admin account. Keep any new example loopback-first.
- **Restart policy:** `restart: always`
- **Hardening:** `security_opt: no-new-privileges:true`, json-file logging capped at 50m x 5, and `cap_drop: [ALL]` with only five capabilities added back. The entrypoint starts as root, recursively chowns/chmods `DATA_DIR` (possibly a foreign-owned volume), then `exec gosu`-switches to `appuser`: `CHOWN`, `DAC_OVERRIDE` and `FOWNER` cover the chown/chmod of the data dir; `SETUID` and `SETGID` cover the gosu uid/gid switch. `no-new-privileges:true` stops any setuid binary from re-raising those.

### Health Check
Defined in the **Dockerfile**, not in compose:

```
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3
  CMD python -c "... urlopen('http://127.0.0.1:' + PORT + '/health', timeout=2)" || exit 1
```

The endpoint is `/health`. There is no `/api/ping`.

## Dependencies

### Internal
- `/pyproject.toml` — dependency source, also copied into the image so `app/utils/version.py` can read the version at runtime
- `/app/`, `/middleware/`, `/run.py` — application source
- `/CHANGELOG.md` — copied in so the UI can render it
- `/tools/pyproject-deps.py` — **not** used by the image build: `tools/` is excluded by `.dockerignore`, and the builder reads `[project].dependencies` inline with `tomllib`. The script is the same step's copy for CI and the release workflow.

### External
- **Base image:** `python:3.13-slim` (overridable via `ARG PYTHON_BASE`)
- **FFmpeg:** static build from BtbN/FFmpeg-Builds
- **Deno:** installed in the builder for yt-dlp-ejs
- **Registry:** Docker Hub. Compose pulls `giiibates/fetchly:${FETCHLY_TAG:-latest}`; the release workflow pushes to `${DOCKERHUB_USERNAME}/fetchly`.

## Manual

### Building Locally
```bash
docker build -f docker/Dockerfile -t fetchly:dev .
```

### Running the Container
```bash
docker run -it \
  -p 8000:8000 \
  -v fetchly-data:/app/data \
  -e FETCHLY_SECRET_KEY="$(openssl rand -base64 32)" \
  giiibates/fetchly:latest
```

### Using Docker Compose
```bash
# .env
FETCHLY_SECRET_KEY=...        # head -c 32 /dev/urandom | base64

docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f fetchly
```

### Multi-Architecture Images
`.github/workflows/docker-build.yml` builds `linux/amd64` and `linux/arm64` with buildx. It resolves the runtime dependency set **once** for both architectures — failing if the two cannot agree on one — and hands the resolved constraints to every architecture build, so the published images carry identical versions.

### Troubleshooting Container Startup
```bash
docker logs <container>        # the entrypoint reports what it refused to do
```
- **Exits immediately:** missing `FETCHLY_SECRET_KEY`, unwritable `/app/data`, or `WORKERS` set to something other than 1
- **Unhealthy:** `/health` not answering — check the app logs, not the health check
- **Reset the database:** `rm data/jobs.db` (the file is `jobs.db`, under `DATA_DIR`), then restart

### Customizing the Image
```dockerfile
# Extra system package — runtime stage:
RUN apt-get install -y --no-install-recommends my-new-package
```
Change the base with `--build-arg PYTHON_BASE=...`; it must stay a Python 3.13 Debian image.

---

**Last Updated:** 2026-10-26  
**Base Image:** `python:3.13-slim`  
**Stages:** ffmpeg, essentia, builder, runtime  
**Platforms:** linux/amd64, linux/arm64  
**Health Endpoint:** `/health` (defined in the Dockerfile)
