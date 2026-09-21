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
  - `builder` — builds the virtualenv, vendors WaveSurfer from unpkg's `@latest` alias (no version `ARG`, no hash pin — see "Vendoring WaveSurfer" below), installs yt-dlp / yt-dlp-ejs / Deno at the versions passed in
  - `runtime` — installs the few runtime apt packages, copies `ffmpeg`/`ffprobe`, the `/venv`, `deno` and the application source
- **Adding a Python dependency:** add it to `pyproject.toml`. The builder reads `[project].dependencies` out of it with `tomllib` and installs that set without installing the project, so the layer stays cached across source-only changes. (`tools/pyproject-deps.py` is the release workflow's copy of the same step, not the builder's.)
- **torch and torchaudio** are the only packages installed from `TORCH_CPU_INDEX`, with `--index-url` and `--no-deps` so that index is never merged into PyPI's namespace for the rest of the manifest — it is not a torch-only host, it also serves `jinja2`, `numpy`, `sympy`, `filelock`, `fsspec` and `setuptools`. Their own dependencies come from PyPI with everything else, and the stage asserts `torch.version.cuda is None` afterwards. `pyproject.toml` expresses the same split for resolvers: `[[tool.uv.index]] pytorch-cpu` is `explicit = true` and `[tool.uv.sources]` binds those two names to it, which is why `torchaudio` is listed in `[project.dependencies]` although nothing imports it — a source binding does not reach a purely transitive dependency.
- **Adding a system package:** extend the `apt-get install` line in the `runtime` stage
- **`APT_CACHE_DATE`** is declared globally *and* redeclared inside `ffmpeg`, `builder` and `runtime`. A global `ARG` is invisible to a stage that does not redeclare it, so a new apt layer must carry its own `ARG APT_CACHE_DATE` line or the value passed with `--build-arg` reaches nothing and `apt-get upgrade` becomes a permanent cache hit. The `essentia` stage deliberately stays out: nothing it installs ships, and keying it on a daily value would recompile essentia and its static 3rd-party chain on every release. The release workflow passes one date for both architectures.
- **`apt-get update`, `apt-get upgrade -y`, install and cleanup stay in one `RUN`** per stage — splitting them reintroduces the stale-index problem the cache buster exists to avoid.
- **yt-dlp:** deliberately unpinned and absent from `pyproject.toml`; the version is supplied by `ARG YTDLP_VERSION` at build time as a **bare version** (`2026.1.1`), not a specifier — the Dockerfile builds the `==` itself and rejects a value that already carries one

### Vendoring WaveSurfer

**Deliberate design decision: "latest is greatest", not a version pin.** The
`builder` stage resolves `https://unpkg.com/wavesurfer.js@latest/package.json`
through `ADD` and then downloads `wavesurfer.esm.js` and `regions.esm.js` from
that one concrete version. There is no `WAVESURFER_VERSION` build `ARG` and no
`WAVESURFER_ESM_SHA256` / `WAVESURFER_REGIONS_SHA256` hash pin — both existed in
earlier revisions of this Dockerfile and were removed on purpose.

- **Why `ADD` and not three `curl` calls in one `RUN`:** a `RUN` layer has no
  reason to be invalidated when upstream publishes — nothing in the build
  context changed — so "every build takes the current release" only held for a
  cold cache. `ADD` from a URL re-checks the remote and invalidates the layer
  when the manifest changes. It is also the single resolution point: three
  independent `@latest` lookups could straddle a release and vendor a bundle,
  a plugin and a reported version from two different ones.

- **Why:** a build-time version pin only freezes a known point in time and has
  to be bumped by hand whenever upstream ships a fix — which is what prompted
  this change: a stale pin left a real upstream fix release undeployed while
  the Settings → System tile kept reporting "Update available". Always taking
  `@latest` means a build always carries the current release, cached or not.
- **What this trades away:** there is no build-time integrity check (SHA-256)
  on the fetched files any more, and two builds run at different times can
  vendor two different WaveSurfer versions from the same Dockerfile — this is
  accepted, not an oversight.
- **Do not expect the Trivy scan to cover it.** The image scan in
  `.github/workflows/docker-build.yml` (not `ci.yml` — that workflow runs no
  scanner) is configured `vuln-type: os,library`, and Trivy detects a Node
  library from a `package.json` or a lockfile. The vendored bundle is two bare
  `.esm.js` files under `app/static/vendor/wavesurfer/dist/` with no manifest
  beside them, so it is invisible to that gate. A compromised or vulnerable
  upstream release is caught by nothing in this pipeline — that is the residual
  risk this decision accepts, and the reason to keep an eye on upstream
  advisories by hand.
- **How the version is still reported:** the `builder` stage resolves the
  version unpkg actually served (`.../package.json`'s `"version"` field) into
  `/build/wavesurfer.version`. The `runtime` stage copies that file to
  `/app/.wavesurfer_version` (deliberately outside `app/static/`, so it is
  never served), and `entrypoint.sh` reads it at container start and exports
  `WAVESURFER_VERSION` — the same environment variable
  `app/utils/version.py`'s `get_wavesurfer_version()` always read. No code in
  `app/` changed for this.
- **Pinning it back:** there is no build arg to override any more. An
  operator who needs a frozen version has to reintroduce the `ARG`/hash pair
  and the `sha256sum -c` step this change removed — that is the supported way
  back, not a flag on today's Dockerfile.

### Working on the Entrypoint
- **`WORKERS` is fixed at 1 and enforced, not merely defaulted.** The job queue *and* the SSE subscriber registry live in process memory with no cross-process coordination. A second Gunicorn worker means the same job processed twice and clients subscribed to a process that never sees their job's events — a correctness failure, not a throughput trade-off. CPU parallelism comes from the governor's semaphores instead.
- **`DATA_DIR` and `TORCH_HOME` are validated before anything privileged runs.** Both reach a root `chown`/`chmod` (`DATA_DIR` a recursive one), so `validate_managed_dir` rejects a relative path and any value resolving to a system directory. `REQUIRED_DIRS` holds `TORCH_HOME` itself and **not** its parent: `mkdir -p` creates the parent anyway, torch only writes below `TORCH_HOME`, and an entry in that array is chown'ed and chmod'ed — `TORCH_HOME=/etc/torch` used to hand `/etc` to `appuser` with mode 700. Do not add a parent directory to that array.
- **Startup sequence:** resolve `TORCH_HOME` (pinned under `DATA_DIR` so the ~81 MB beat-this checkpoint survives a container recreate) and `WAVESURFER_VERSION` (read from `/app/.wavesurfer_version`, written by the `builder` stage — see "Vendoring WaveSurfer" above) → create and chown required directories → `exec gosu "$APP_USER"` → `exec gunicorn app.main:app --workers "$WORKERS" --worker-class uvicorn_worker.UvicornWorker --logger-class app.gunicorn_logging.FetchlyGunicornLogger`
- **No admin account is created** at startup, and **no separate worker process is spawned** — the worker runs inside the app process
- **Database setup** happens in the FastAPI lifespan hook, not in the entrypoint
- **Graceful shutdown:** `GRACEFUL_TIMEOUT` bounds how long Gunicorn waits for the lifespan shutdown (stop workers, WAL checkpoint, close SQLite). It must stay below the orchestrator's kill grace period — compose sets `stop_grace_period: 20s` — so the checkpoint actually completes.
- **Logging:** stdout/stderr

### Working on Docker Compose
- **Volume:** `./data:/app/data` — database, downloads, cookie jars, caches
- **Required:** `FETCHLY_SECRET_KEY`; compose fails fast with `:?required` if it is unset
- **Variables it reads:** `FETCHLY_TAG`, `FETCHLY_BIND`, `FETCHLY_PORT`, `FETCHLY_SECRET_KEY`, `LOG_LEVEL`, `TZ`, `TIMEOUT`
- **Port publishing:** `"${FETCHLY_BIND:-127.0.0.1}:${FETCHLY_PORT:-8000}:8000"`. The host address is not optional decoration — without it Docker publishes on every interface, and a fresh install has no admin account. Keep any new example loopback-first.
- **Restart policy:** `restart: always`
- **Hardening:** `security_opt: no-new-privileges:true`, json-file logging capped at 50m x 5

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

**Last Updated:** 2026-09-19  
**Base Image:** `python:3.13-slim`  
**Stages:** ffmpeg, essentia, builder, runtime  
**Platforms:** linux/amd64, linux/arm64  
**Health Endpoint:** `/health` (defined in the Dockerfile)
