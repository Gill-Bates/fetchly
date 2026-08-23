# syntax=docker/dockerfile:1.7

# The base image for every stage. Floating by default so security updates of
# the Debian base arrive with a rebuild; a release pipeline that needs
# byte-reproducible images passes an approved digest instead:
#   --build-arg PYTHON_BASE=python:3.13-slim@sha256:<digest>
# The same applies to the deliberately rolling toolchain below - FFMPEG_SERIES,
# YTDLP_VERSION, YTDLP_EJS_VERSION, DENO_VERSION and WAVESURFER_VERSION all
# accept an explicit pin, so "reproducible" is a build-args decision rather
# than a fork of this file.
ARG PYTHON_BASE=python:3.13-slim

# --------------------------------------------------------------------------- #
# Stage: ffmpeg - upstream static build, never the distro package
# --------------------------------------------------------------------------- #
# ffmpeg/ffprobe are core functionality here, so the image must not inherit
# whatever Debian froze at its release - Debian stable intentionally keeps a
# fixed ffmpeg release line for the life of the release. BtbN publishes
# statically linked Linux builds under a rolling
# "latest" release tag, in two flavours: "master" (nightly builds of ffmpeg's
# development branch) and "nX.Y" (the release/X.Y maintenance branch, i.e. the
# stable X.Y release plus its backported bugfixes). This image deliberately
# takes the stable one - no master, no pre-release.
#
# FFMPEG_SERIES selects it. Left empty, the newest stable series present in the
# release is detected at build time, so a new stable major is picked up without
# editing this file; set it explicitly (e.g. --build-arg FFMPEG_SERIES=8.1) to
# stay on an older line.
#
# The download is verified against the checksums.sha256 published with the same
# release. That catches a truncated or corrupted transfer - it cannot, by
# construction, pin a rolling artifact to a known-good hash.
FROM ${PYTHON_BASE} AS ffmpeg

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH
ARG FFMPEG_SERIES=
# The license variant of the static build - gpl or lgpl. BtbN also publishes
# "-shared" variants (dynamically linked against bundled libav*.so files);
# those are deliberately not accepted here, since only the two ffmpeg/ffprobe
# binaries are copied into the runtime image, not their shared libraries.
ARG FFMPEG_VARIANT=gpl
ARG FFMPEG_RELEASE_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest

COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    xz-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Serves two purposes: it lists every asset in the release (which is how the
# newest stable series is discovered below), and it doubles as the layer cache
# key. The tarball URL is a fixed name under a rolling tag, so a plain RUN
# would stay cached forever and keep shipping the ffmpeg build that happened to
# be current on the first build. This 5 KB file is refetched on every build and
# changes exactly when BtbN publishes new ones, so the download below reruns
# then and only then.
ADD ${FFMPEG_RELEASE_URL}/checksums.sha256 /tmp/checksums.sha256

# TARGETARCH is refused rather than defaulted: an empty value (legacy builder,
# BuildKit disabled) would silently select the amd64 artifact and produce an
# image whose binaries cannot execute on arm64.
RUN set -euo pipefail; \
    : "${TARGETARCH:?must be provided by BuildKit - build with DOCKER_BUILDKIT=1 or docker buildx}"; \
    case "${TARGETARCH}" in \
        amd64) ffmpeg_arch=linux64 ;; \
        arm64) ffmpeg_arch=linuxarm64 ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    case "${FFMPEG_VARIANT}" in \
        gpl|lgpl) ;; \
        *) echo "FFMPEG_VARIANT must be gpl or lgpl (statically linked), got: ${FFMPEG_VARIANT}" >&2; exit 1 ;; \
    esac; \
    series="${FFMPEG_SERIES}"; \
    if [[ -z "${series}" ]]; then \
        series="$(awk '{print $2}' /tmp/checksums.sha256 \
            | sed -n "s/^ffmpeg-n\([0-9][0-9.]*\)-latest-${ffmpeg_arch}-${FFMPEG_VARIANT}-\1\.tar\.xz$/\1/p" \
            | sort -V | tail -n 1)"; \
    fi; \
    if [[ -z "${series}" ]]; then \
        echo "no stable ffmpeg series found for ${ffmpeg_arch}/${FFMPEG_VARIANT}" >&2; exit 1; \
    fi; \
    echo "Selected stable ffmpeg series: n${series}"; \
    tarball="ffmpeg-n${series}-latest-${ffmpeg_arch}-${FFMPEG_VARIANT}-${series}.tar.xz"; \
    cd /tmp; \
    curl --proto '=https' --tlsv1.2 -L --no-progress-meter "${FFMPEG_RELEASE_URL}/${tarball}" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 --max-time 300 -o "${tarball}"; \
    checksum_line="$(awk -v f="${tarball}" '$2 == f' /tmp/checksums.sha256)"; \
    [[ "$(printf '%s\n' "${checksum_line}" | grep -c .)" -eq 1 ]] || \
        { echo "expected exactly one checksum entry for ${tarball}" >&2; exit 1; }; \
    printf '%s\n' "${checksum_line}" | sha256sum -c -; \
    mkdir -p /opt/ffmpeg/bin; \
    tar -xJf "${tarball}" -C /opt/ffmpeg/bin --strip-components=2 \
        --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'; \
    rm -f "${tarball}" /tmp/checksums.sha256; \
    /opt/ffmpeg/bin/ffmpeg -version | head -n 1; \
    /opt/ffmpeg/bin/ffprobe -version | head -n 1


FROM ${PYTHON_BASE} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH

# Exact pin (not a floating major range) plus checksums so a tampered/altered
# unpkg release cannot silently end up in the image and run in users' browsers.
ARG WAVESURFER_VERSION=7.12.11
ARG WAVESURFER_ESM_SHA256=49fc9b562f0c0322882aba6fdf354d54e4c81470611a50771bbd1fd398631e13
ARG WAVESURFER_REGIONS_SHA256=14780f7d3f7cabfc4fb9a37ddf81c4fcc1045cfdb0b93c4a8c4c6acd52d03a7f

# Install builder dependencies without recommends.
# git is required: requirements.txt pulls madmom via git+https.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    build-essential \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

ENV VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH"

# Create venv and install Python dependencies.
COPY requirements.txt ./requirements.txt
RUN python -m venv "$VIRTUAL_ENV" && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# yt-dlp from PyPI, never the distro package: the app both shells out to the
# `yt-dlp` binary and imports yt_dlp as a library, so it has to live in the
# venv rather than being a standalone binary.
#
# Extractors break whenever YouTube/TikTok/Instagram change something, so a
# cached layer here would quietly ship a stale yt-dlp forever. The PyPI
# metadata document is fetched on every build and its content is the layer's
# cache key: identical while no new yt-dlp release exists, different the
# moment one appears - which then reinstalls yt-dlp and nothing else. It is
# ADDed in this stage (not the runtime stage) so the metadata itself never
# ends up in the final image.
#
# Stable releases only - the same ones on yt-dlp's GitHub release page. pip
# ignores pre-releases unless asked with --pre, and yt-dlp's nightly channel
# lives in a separate repository that is not installed from here. Pin with
# --build-arg YTDLP_VERSION="==2026.08.19" if a release ever regresses.
#
# yt-dlp-ejs is installed alongside it: upstream lists it as required for full
# YouTube support, and it follows the same staleness argument, so its own PyPI
# metadata document is ADDed as a second cache key. It carries its own pin
# (--build-arg YTDLP_EJS_VERSION="==0.8.0"), because an incompatible ejs
# release must be rollbackable without also freezing yt-dlp itself.
ARG YTDLP_VERSION=""
ARG YTDLP_EJS_VERSION=""
ADD https://pypi.org/pypi/yt-dlp/json /tmp/yt-dlp-pypi.json
ADD https://pypi.org/pypi/yt-dlp-ejs/json /tmp/yt-dlp-ejs-pypi.json
RUN pip install --no-cache-dir --upgrade \
        "yt-dlp${YTDLP_VERSION}" \
        "yt-dlp-ejs${YTDLP_EJS_VERSION}" && \
    rm -f /tmp/yt-dlp-pypi.json /tmp/yt-dlp-ejs-pypi.json && \
    yt-dlp --version

# deno - the JavaScript runtime that executes yt-dlp-ejs. yt-dlp enables only
# "deno" by default and picks it up from PATH, so no yt-dlp flag is involved;
# the binary simply has to be in the runtime image. Without it, yt-dlp-ejs is
# installed but inert and YouTube extraction falls back to the degraded path.
#
# Left empty, the newest stable release is used; pin with --build-arg
# DENO_VERSION=v2.9.5 if one regresses. The release marker is ADDed rather
# than curl'ed inside the RUN for the same reason as the yt-dlp metadata
# above: Docker compares only the instruction text when deciding whether a
# RUN is cached, so resolving the version inside it would freeze deno at
# whatever was current on the first build. Fetching the marker as its own
# layer makes its content the cache key, so a new deno release - and nothing
# else - reruns the download below.
#
# The archive is verified against the .sha256sum published with the same
# release - that catches a corrupted transfer, not a rolling-tag substitution.
ARG DENO_VERSION=""
ADD https://dl.deno.land/release-latest.txt /tmp/deno-release-latest.txt
RUN set -euo pipefail; \
    : "${TARGETARCH:?must be provided by BuildKit - build with DOCKER_BUILDKIT=1 or docker buildx}"; \
    case "${TARGETARCH}" in \
        amd64) deno_arch=x86_64-unknown-linux-gnu ;; \
        arm64) deno_arch=aarch64-unknown-linux-gnu ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    tag="${DENO_VERSION:-$(tr -d " \t\r\n" < /tmp/deno-release-latest.txt)}"; \
    [[ -n "${tag}" ]] || { echo "empty deno release tag" >&2; exit 1; }; \
    base="https://github.com/denoland/deno/releases/download/${tag}"; \
    archive="deno-${deno_arch}.zip"; \
    curl --proto '=https' --tlsv1.2 -fsSL --retry 5 --retry-delay 2 \
        --connect-timeout 5 --max-time 300 -o "/tmp/${archive}" "${base}/${archive}"; \
    curl --proto '=https' --tlsv1.2 -fsSL --retry 5 --retry-delay 2 \
        --connect-timeout 5 --max-time 60 -o /tmp/deno.sha256sum "${base}/${archive}.sha256sum"; \
    (cd /tmp && sha256sum -c deno.sha256sum); \
    unzip -q "/tmp/${archive}" -d /usr/local/bin; \
    chmod 755 /usr/local/bin/deno; \
    rm -f "/tmp/${archive}" /tmp/deno.sha256sum /tmp/deno-release-latest.txt; \
    deno --version

# Fetch pinned WaveSurfer assets and verify their integrity.
RUN mkdir -p /build/static/vendor/wavesurfer/dist/plugins && \
    curl --proto '=https' --tlsv1.2 -L "https://unpkg.com/wavesurfer.js@${WAVESURFER_VERSION}/dist/wavesurfer.esm.js" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 --max-time 300 \
        -o /build/static/vendor/wavesurfer/dist/wavesurfer.esm.js && \
    curl --proto '=https' --tlsv1.2 -L "https://unpkg.com/wavesurfer.js@${WAVESURFER_VERSION}/dist/plugins/regions.esm.js" \
        --fail --retry 5 --retry-delay 2 --connect-timeout 5 --max-time 300 \
        -o /build/static/vendor/wavesurfer/dist/plugins/regions.esm.js && \
    echo "${WAVESURFER_ESM_SHA256}  /build/static/vendor/wavesurfer/dist/wavesurfer.esm.js" | sha256sum -c - && \
    echo "${WAVESURFER_REGIONS_SHA256}  /build/static/vendor/wavesurfer/dist/plugins/regions.esm.js" | sha256sum -c -


FROM ${PYTHON_BASE} AS runtime

# ARGs don't cross FROM stages, so the pin from the builder stage above is
# redeclared here purely to expose it to the running app (Settings page
# "installed versions" tile) - the vendored bundle itself carries no
# version marker to read at runtime.
ARG WAVESURFER_VERSION=7.12.11
ENV WAVESURFER_VERSION=${WAVESURFER_VERSION}

# Propagate failures from every command in a shell pipeline, not just its
# last stage (plain `sh -c` would otherwise hide a failed curl behind a
# successful downstream command).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Block the distro ffmpeg/yt-dlp packages before anything can install them.
# Copied in ahead of the apt-get below so no dependency, recommendation, or
# later `apt-get install` in a derived image can shadow the upstream builds
# that this image deliberately ships (see the file for details).
COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

# Install runtime dependencies without recommends to keep image slim.
# bash is already present in python:3.13-slim; curl is not needed since the
# healthcheck below uses the Python already present in the image. ffmpeg is
# NOT here - it comes from the static upstream build in the ffmpeg stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Static ffmpeg/ffprobe from the upstream build. /usr/local/bin comes before
# /usr/bin in PATH, so even a distro ffmpeg that somehow got installed anyway
# would not win.
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/

WORKDIR /app

# Copy the prebuilt virtualenv from the builder stage.
COPY --from=builder /venv /venv

# The JavaScript runtime for yt-dlp-ejs (installed into the venv above).
# yt-dlp resolves "deno" from PATH on its own - no configuration is needed.
COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno

ENV VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH"

# yt-dlp ships inside the venv copied above (installed in the builder stage).
# Verify the whole external toolchain resolves from PATH and record the exact
# versions in the build log. app/main.py's own startup check only covers
# ffmpeg and yt-dlp (not ffprobe), so this build-time check is what actually
# guarantees ffprobe (used for media inspection) is present and runnable.
#
# `pip check` catches a venv copied into an image whose dependency graph no
# longer resolves consistently (e.g. a version conflict introduced by the
# yt-dlp upgrade above), but it only reads metadata - it cannot tell whether a
# compiled extension finds its shared libraries. The import below does: the
# application imports these lazily, deep inside the BPM path (app/bpm.py,
# app/bpm_beat_this.py), so a venv whose C extensions fail to load would
# otherwise build and start fine and only break on the first analysis job.
RUN python -m pip check && \
    python -c "import numpy, torch, essentia.standard, yt_dlp" && \
    ffmpeg -version | head -n 1 && \
    ffprobe -version | head -n 1 && \
    echo "yt-dlp $(yt-dlp --version)" && \
    echo "yt-dlp-ejs $(python -c 'from importlib import metadata; print(metadata.version("yt-dlp-ejs"))')" && \
    deno --version | head -n 1 && \
    [[ "$(command -v ffmpeg)" == "/usr/local/bin/ffmpeg" ]]

# madmom and beat-this are the two dependencies the application itself treats
# as optional: app/bpm.py falls back to an Essentia-only result and
# app/bpm_beat_this.py disables DBN postprocessing when they cannot be
# imported. Failing the build over them would be stricter than the runtime
# contract, so their status is recorded in the build log instead - a WARNING
# here means the image ships with degraded BPM accuracy.
RUN python <<'PYCHECK'
import importlib

for name in ("madmom", "beat_this.inference"):
    try:
        importlib.import_module(name)
    except Exception as exc:
        print(f"WARNING: optional dependency {name} unavailable: {exc!r}")
    else:
        print(f"optional dependency {name}: ok")
PYCHECK

# Create the runtime user. No USER instruction follows: the entrypoint starts
# as root so it can fix ownership of a mounted /app/data volume, then drops to
# this user via gosu before exec'ing gunicorn - see docker/entrypoint.sh.
RUN useradd -u 1000 -m --no-log-init appuser

# Copy application files from the local build context. Deliberately left
# root-owned: appuser needs to read this code, never to write it, so a
# compromised application process cannot rewrite its own Python modules,
# Jinja templates, or the JavaScript served to browsers. Only /app/data below
# is made writable. PYTHONDONTWRITEBYTECODE=1 keeps Python from wanting to
# create __pycache__ directories next to the sources.
COPY app ./app
COPY middleware ./middleware
# BUILD_INFO is gitignored and only exists when the release recipe in
# setup.conf writes it (git SHA) right before this build. A wildcard COPY
# tolerates its absence in a plain "docker build ." from a fresh checkout -
# app/utils/version.py already falls back to "dev" when the file is missing,
# so this simply lets the build honour that same fallback instead of failing.
COPY VERSION BUILD_INFO* ./
COPY --from=builder /build/static/vendor/wavesurfer ./app/static/vendor/wavesurfer
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh

# .gitattributes normalizes this file to LF, but that only guards `git`
# checkouts - a CRLF could still reach here via a raw file copy or a build
# context assembled by other means. A CRLF shebang line is a broken
# interpreter path, which would surface only when the container tries to
# start; catch it here instead, at build time.
RUN bash -n /entrypoint.sh && \
    ! grep -q $'\r' /entrypoint.sh

# Python runtime hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Prepare the image-baked default data directory. If a volume or bind mount
# is attached at /app/data instead, its ownership is handled separately by
# the entrypoint's bootstrap step at container start.
RUN mkdir -p /app/data/downloads && \
    chown -R appuser:appuser /app/data

# WORKERS=1: the job queue and the SSE status broadcast (app/main.py) live in
# process memory with no cross-process coordination, so more than one
# Gunicorn worker means duplicate job processing and clients missing live
# updates - this is not merely a performance trade-off but an incorrect
# operating mode without an external queue/pub-sub. CPU parallelism for
# downloads/transcodes/analysis already comes from application-managed
# worker threads bounded by Governor semaphores (app/governor.py),
# independently of this setting.
ENV DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKERS=1 \
    TIMEOUT=60

EXPOSE 8000

# Health check for orchestration. Uses 127.0.0.1 (not localhost) to avoid
# hostname resolution and guarantee the probe reaches the IPv4 listener, and
# respects a runtime-overridden PORT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/health', timeout=2)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
