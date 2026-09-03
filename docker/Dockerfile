# syntax=docker/dockerfile:1.7

ARG PYTHON_BASE=python:3.13-slim

FROM ${PYTHON_BASE} AS ffmpeg

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH
ARG FFMPEG_SERIES=
ARG FFMPEG_VARIANT=gpl
ARG FFMPEG_RELEASE_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest

COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    xz-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ADD ${FFMPEG_RELEASE_URL}/checksums.sha256 /tmp/checksums.sha256

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

FROM ${PYTHON_BASE} AS essentia

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH
ARG ESSENTIA_REPO=https://github.com/MTG/essentia.git
ARG ESSENTIA_REF=master

COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

ADD https://github.com/MTG/essentia/commits/${ESSENTIA_REF}.atom /tmp/essentia-ref.atom

RUN set -euo pipefail; \
    : "${TARGETARCH:?must be provided by BuildKit - build with DOCKER_BUILDKIT=1 or docker buildx}"; \
    mkdir -p /wheels; \
    if [[ "${TARGETARCH}" != "arm64" ]]; then \
        echo "TARGETARCH=${TARGETARCH}: essentia ships an upstream wheel, nothing to build here"; \
        exit 0; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        nasm \
        pkg-config; \
    rm -rf /var/lib/apt/lists/*; \
    git clone --filter=blob:none "${ESSENTIA_REPO}" /src/essentia; \
    cd /src/essentia; \
    git checkout --detach "${ESSENTIA_REF}"; \
    echo "essentia source: $(git describe --always --tags)"; \
    printf '\n%s\n%s\n' \
        'FFTW_VERSION=fftw-3.3.10' \
        'FFTW_FLAGS="--enable-float --enable-neon"' >> packaging/build_config.sh; \
    printf 'ac_cv_build=aarch64-unknown-linux-gnu\nac_cv_host=aarch64-unknown-linux-gnu\n' \
        > /tmp/config.site; \
    export CONFIG_SITE=/tmp/config.site; \
    export MAKEFLAGS="-j$(nproc)"; \
    pip wheel --no-cache-dir --no-deps -w /wheels .; \
    rm -f /tmp/essentia-ref.atom; \
    ls -l /wheels

FROM ${PYTHON_BASE} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH

ARG WAVESURFER_VERSION=7.12.11
ARG WAVESURFER_ESM_SHA256=49fc9b562f0c0322882aba6fdf354d54e4c81470611a50771bbd1fd398631e13
ARG WAVESURFER_REGIONS_SHA256=14780f7d3f7cabfc4fb9a37ddf81c4fcc1045cfdb0b93c4a8c4c6acd52d03a7f

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    build-essential \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

ENV VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# pyproject.toml is the single dependency manifest (it replaced
# requirements.txt). Only the [project] dependencies are materialised below -
# the project itself is never installed, so this layer stays cached across
# source-only changes the way the old requirements.txt layer did.
COPY pyproject.toml ./pyproject.toml

COPY --from=essentia /wheels /tmp/essentia-wheels

# beat-this depends on torch; without this index pip resolves the CUDA build
# and the image grows by several gigabytes for GPU code that never runs.
ENV TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu

RUN set -euo pipefail; \
    python -m venv "$VIRTUAL_ENV"; \
    pip install --upgrade pip; \
    shopt -s nullglob; \
    essentia_wheels=(/tmp/essentia-wheels/essentia-*.whl); \
    if (( ${#essentia_wheels[@]} > 0 )); then \
        echo "Installing locally built essentia: ${essentia_wheels[*]}"; \
        pip install --no-cache-dir "${essentia_wheels[@]}"; \
    fi; \
    python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' \
        > /tmp/requirements.txt; \
    cat /tmp/requirements.txt; \
    pip install --no-cache-dir --extra-index-url "$TORCH_CPU_INDEX" -r /tmp/requirements.txt; \
    rm -f /tmp/requirements.txt; \
    rm -rf /tmp/essentia-wheels

ARG YTDLP_VERSION=""
ARG YTDLP_EJS_VERSION=""
ADD https://pypi.org/pypi/yt-dlp/json /tmp/yt-dlp-pypi.json
ADD https://pypi.org/pypi/yt-dlp-ejs/json /tmp/yt-dlp-ejs-pypi.json
RUN pip install --no-cache-dir --upgrade \
        "yt-dlp${YTDLP_VERSION}" \
        "yt-dlp-ejs${YTDLP_EJS_VERSION}" && \
    rm -f /tmp/yt-dlp-pypi.json /tmp/yt-dlp-ejs-pypi.json && \
    yt-dlp --version

RUN set -euo pipefail; \
    site_packages="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    python -m pip check; \
    find "$VIRTUAL_ENV" -type d -name __pycache__ -prune -exec rm -rf '{}' +; \
    find "$site_packages" -depth -type d \( -name test -o -name tests \) -exec rm -rf '{}' +; \
    rm -rf "$site_packages/torch/include"; \
    find "$site_packages/torch/bin" -maxdepth 1 -type f \
        \( -name 'test*' -o -name '*Test' -o -name 'protoc*' \) -delete; \
    test -x "$site_packages/torch/bin/torch_shm_manager"

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

ARG WAVESURFER_VERSION=7.12.11
ENV WAVESURFER_VERSION=${WAVESURFER_VERSION}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    gosu \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/

WORKDIR /app

COPY --from=builder /venv /venv

COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno

ENV VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip check && \
    python -c "import numpy, torch, essentia.standard, yt_dlp" && \
    ffmpeg -version | head -n 1 && \
    ffprobe -version | head -n 1 && \
    echo "yt-dlp $(yt-dlp --version)" && \
    echo "yt-dlp-ejs $(python -c 'from importlib import metadata; print(metadata.version("yt-dlp-ejs"))')" && \
    deno --version | head -n 1 && \
    [[ "$(command -v ffmpeg)" == "/usr/local/bin/ffmpeg" ]]

RUN python <<'PYCHECK'
import importlib

for name in ("beat_this.inference",):
    try:
        importlib.import_module(name)
    except Exception as exc:
        print(f"WARNING: optional dependency {name} unavailable: {exc!r}")
    else:
        print(f"optional dependency {name}: ok")
PYCHECK

RUN useradd -u 1000 -m --no-log-init appuser

COPY app ./app
COPY middleware ./middleware
RUN chmod -R a+rX ./app ./middleware
RUN test -s ./app/static/img/fetchly_watermark.png && \
    test -s ./app/static/fonts/roboto-flex.ttf && \
    ffmpeg -hide_banner -v error \
        -f lavfi -i "color=c=black:s=16x16:d=0.04" \
        -vf "drawtext=fontfile=/app/app/static/fonts/roboto-flex.ttf:text=fetchly" \
        -frames:v 1 -f null -
# app/utils/version.py reads [project] version straight out of pyproject.toml,
# so the manifest ships next to the source rather than a separate VERSION file.
COPY pyproject.toml BUILD_INFO* ./
COPY CHANGELOG.md ./
COPY --from=builder /build/static/vendor/wavesurfer ./app/static/vendor/wavesurfer
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh

RUN bash -n /entrypoint.sh && \
    ! grep -q $'\r' /entrypoint.sh

RUN mkdir -p /app/data/downloads /app/data/cookies && \
    chown -R appuser:appuser /app/data

ENV DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKERS=1 \
    TIMEOUT=60

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/health', timeout=2)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
