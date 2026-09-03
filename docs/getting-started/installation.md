# Installation

fetchly ships as a single Docker image that bundles every runtime dependency:
`ffmpeg`, `yt-dlp`, `yt-dlp-ejs`, `deno`, Essentia, and the `beat_this` beat tracker.
Docker is the supported and recommended way to run it.

## Docker (recommended)

### Image

| | |
|---|---|
| Registry | [`giiibates/fetchly`](https://hub.docker.com/r/giiibates/fetchly) |
| Platforms | `linux/amd64`, `linux/arm64` |
| Exposed port | `8000` |
| Data volume | `/app/data` |
| Health check | `GET /health` every 30 s |

### Compose file

A fuller Compose file with logging, timezone, and hardening options ships in the
repository at [`docker/docker-compose.yml`](https://github.com/Gill-Bates/fetchly/blob/main/docker/docker-compose.yml):

```yaml title="docker/docker-compose.yml"
services:
  fetchly:
    image: giiibates/fetchly:${FETCHLY_TAG:-latest}
    container_name: fetchly
    restart: always
    stop_grace_period: 20s
    ports:
      - "${FETCHLY_PORT:-8000}:8000"
    environment:
      LOG_LEVEL: ${LOG_LEVEL:-info}
      TZ: ${TZ:-Etc/UTC}
      TIMEOUT: 60
      FETCHLY_SECRET_KEY: "${FETCHLY_SECRET_KEY:?required}"
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
    security_opt:
      - no-new-privileges:true
    volumes:
      - ./data:/app/data
```

Create an `.env` file next to it:

```bash title=".env"
FETCHLY_SECRET_KEY=<output of: openssl rand -base64 32>
TZ=Europe/Berlin
LOG_LEVEL=info
```

```bash
docker compose up -d
```

!!! tip "stop_grace_period"
    Keep `stop_grace_period` above the container's graceful shutdown budget
    (`GRACEFUL_TIMEOUT`, 15 s by default). The shutdown path stops the worker threads
    and checkpoints the SQLite WAL; killing it early can leave the WAL uncheckpointed.

### Building the image yourself

The Dockerfile is multi-stage and multi-architecture, and requires BuildKit:

```bash
DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile -t fetchly .
```

!!! warning "arm64 builds are slow"
    On arm64 there is no prebuilt Essentia wheel, so the image compiles it from
    source. Expect a long build; use the published image where you can.

## Standalone (without Docker)

Supported for development and for hosts where Docker is not an option.

### Requirements

| Requirement | Notes |
|---|---|
| Linux | The host-stats and process handling are Linux-first |
| Python 3.13 | The version the image is built against |
| `ffmpeg` | On `PATH` — transcoding, trimming, waveform peaks |
| `yt-dlp` | On `PATH` |
| `deno` | On `PATH` — solves YouTube JS challenges via `yt-dlp-ejs` |

### Steps

```bash
git clone https://github.com/Gill-Bates/fetchly.git
cd fetchly

python3.13 -m venv .venv
source .venv/bin/activate

pip install --extra-index-url https://download.pytorch.org/whl/cpu -e .
pip install yt-dlp yt-dlp-ejs

export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
python run.py
```

`run.py` binds `HOST:PORT` (`0.0.0.0:8000` by default) and writes its data to `data/`
relative to the working directory unless `DATA_DIR` says otherwise.

!!! note "Optional dependencies degrade gracefully"
    Essentia and `beat_this` are only needed for [BPM analysis](../features/bpm.md).
    If they are missing, downloads still work and analysis is skipped.

## First-run downloads

The first BPM analysis downloads the `beat_this` model checkpoint (~81 MB) from
`cloud.cp.jku.at`. The container pins `TORCH_HOME` to `${DATA_DIR}/.cache/torch`, so
the download survives container recreation as long as the volume does.

## Upgrading

=== "Docker Compose"

    ```bash
    docker compose pull
    docker compose up -d
    ```

=== "docker run"

    ```bash
    docker pull giiibates/fetchly:latest
    docker rm -f fetchly
    # re-run your original docker run command
    ```

The database schema migrates automatically at startup. **Settings → System** shows the
running version alongside the latest published release and the versions of the bundled
tools.

!!! tip "Back up before upgrading"
    Everything that matters lives in the mounted data volume. Stop the container and
    copy the directory:

    ```bash
    docker compose stop
    tar czf fetchly-backup-$(date +%F).tar.gz data/
    docker compose start
    ```

## Uninstalling

```bash
docker compose down
rm -rf data/   # deletes every job, download, cookie jar, and setting
```
