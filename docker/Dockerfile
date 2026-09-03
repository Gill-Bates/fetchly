# syntax=docker/dockerfile:1.7

ARG PYTHON_BASE=python:3.13-slim

FROM ${PYTHON_BASE} AS ffmpeg

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH
# Empty picks the newest stable series the release carries; set it to e.g. 9.0
# to hold one. Only release-branch tarballs qualify:
# 'ffmpeg-n<version>-<arch>-<variant>-<series>.tar.xz'. The <version> field
# reads 'n9.0-latest' in the rolling release and 'n9.0.1-11-ge47273f4d9' in a
# dated autobuild, so the selection below keys on the trailing series field
# instead - the nightly master builds carry none, and the '-shared-' variants
# no digits there, so neither can be selected by accident.
ARG FFMPEG_SERIES=
ARG FFMPEG_VARIANT=gpl
# BtbN's 'latest' release is a rolling tag: its assets are replaced by every
# nightly rebuild, so two architecture builds minutes apart can download two
# different ffmpeg builds under the same file name. The release workflow
# therefore resolves the newest dated autobuild-* release - whose assets are
# immutable - and passes it here, so both architectures extract the same binary.
# A plain `docker build` keeps taking 'latest'.
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
    candidates="$(awk '{print $2}' /tmp/checksums.sha256 \
        | sed -n "s/^\(ffmpeg-n.*-${ffmpeg_arch}-${FFMPEG_VARIANT}-\([0-9][0-9.]*\)\.tar\.xz\)$/\2 \1/p" \
        | sort -V)"; \
    if [[ -n "${FFMPEG_SERIES}" ]]; then \
        tarball="$(awk -v want="${FFMPEG_SERIES}" '$1 == want { print $2 }' <<< "${candidates}" | tail -n 1)"; \
    else \
        tarball="$(tail -n 1 <<< "${candidates}" | awk '{print $2}')"; \
    fi; \
    if [[ -z "${tarball}" ]]; then \
        echo "no stable ffmpeg build for ${ffmpeg_arch}/${FFMPEG_VARIANT}${FFMPEG_SERIES:+ series n${FFMPEG_SERIES}} in ${FFMPEG_RELEASE_URL}" >&2; \
        exit 1; \
    fi; \
    echo "Selected stable ffmpeg build: ${tarball}"; \
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
# Upstream's current master, not a frozen commit. Which is also what makes this
# stage work at all on amd64: essentia only bumped its bundled ffmpeg from
# 2.8.12 (2017) to 7.1.1 in December 2025, and the 2017 tree's x86 inline
# assembly no longer assembles under a current gcc - `operand type mismatch for
# 'shr'` out of libavcodec/x86/mathops.h. Anything older than that bump cannot
# be compiled here on x86 at all, whatever its version number says.
#
# The release workflow resolves this ref to one commit SHA and passes that same
# SHA to both architecture builds, so 'latest' is picked once per release
# rather than twice - see .github/workflows/docker-build.yml. A plain
# `docker build` just takes master's HEAD.
ARG ESSENTIA_REF=master

COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

# Cache buster: ESSENTIA_REF is a moving ref by default, so nothing in the
# build context changes when upstream lands a commit. This feed does.
ADD https://github.com/MTG/essentia/commits/${ESSENTIA_REF}.atom /tmp/essentia-ref.atom

# Both architectures compile essentia here; neither installs it from PyPI.
# Upstream's wheels cannot produce one essentia across a multi-arch release -
# there has never been a linux/aarch64 wheel, and the newest cp313 x86_64 wheel
# is a different build of the library than anything this stage could produce
# for arm64. Taking "the wheel where one exists" would mean publishing two
# differently-built essentias under a single tag. Compiling from one pinned
# commit everywhere costs amd64 the build time - cached, and only paid when
# ESSENTIA_REF moves - and buys identical rhythm analysis on both.
#
# The one thing that cannot be shared is FFTW's SIMD backend, which has no
# portable spelling. Its version is held at 3.3.10 on both: upstream's default
# of 3.3.2 predates aarch64 and is a decade behind on toolchain compatibility,
# so pinning it forward keeps the two builds differing in the flag alone.
RUN set -euo pipefail; \
    : "${TARGETARCH:?must be provided by BuildKit - build with DOCKER_BUILDKIT=1 or docker buildx}"; \
    case "${TARGETARCH}" in \
        amd64) fftw_simd=--enable-sse2 ;; \
        arm64) fftw_simd=--enable-neon ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /wheels; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        file \
        git \
        nasm \
        pkg-config; \
    rm -rf /var/lib/apt/lists/*; \
    git clone --filter=blob:none "${ESSENTIA_REPO}" /src/essentia; \
    cd /src/essentia; \
    git checkout --detach "${ESSENTIA_REF}"; \
    echo "essentia source: $(git describe --always --tags) (${TARGETARCH}, FFTW ${fftw_simd})"; \
    printf '\nFFTW_VERSION=%s\nFFTW_FLAGS="%s %s"\n' \
        fftw-3.3.10 --enable-float "${fftw_simd}" >> packaging/build_config.sh; \
    if [[ "${TARGETARCH}" == "arm64" ]]; then \
        printf 'ac_cv_build=aarch64-unknown-linux-gnu\nac_cv_host=aarch64-unknown-linux-gnu\n' \
            > /tmp/config.site; \
        export CONFIG_SITE=/tmp/config.site; \
    fi; \
    export MAKEFLAGS="-j$(nproc)"; \
    pip wheel --no-cache-dir --no-deps -w /wheels .; \
    rm -f /tmp/essentia-ref.atom; \
    ls -l /wheels; \
    shopt -s nullglob; \
    built=(/wheels/essentia-*.whl); \
    (( ${#built[@]} == 1 )) || \
        { echo "expected exactly one essentia wheel, got: ${built[*]:-none}" >&2; exit 1; }; \
    echo "built $(basename "${built[0]}") from ${ESSENTIA_REF}"

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
#
# constraints.txt is optional and never committed: the release workflow resolves
# the whole transitive dependency set once, for both target platforms, and drops
# the result into the build context of every architecture job. Nothing in
# pyproject.toml is pinned, so this file is what makes two builds started
# minutes apart install the same versions - and, because it sits in the same
# layer as the manifest, what makes their layer caches invalidate together
# instead of one arch reusing an older resolution than the other. Without it -
# a plain local `docker build` - pip simply resolves the newest set itself.
COPY pyproject.toml constraints*.txt ./

COPY --from=essentia /wheels /tmp/essentia-wheels

# beat-this depends on torch; without this index pip resolves the CUDA build
# and the image grows by several gigabytes for GPU code that never runs.
ENV TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu

# essentia is the one dependency pip never resolves here: the wheel was
# compiled from ESSENTIA_REF in the essentia stage above - on every
# architecture, so both halves of the multi-arch manifest carry the same build
# - and is installed from that wheel before the rest of the manifest is read.
# Its requirement line is then dropped rather than re-resolved, so pip cannot
# pull the PyPI wheel over the top of it on the one architecture where such a
# wheel exists.
#
# A missing wheel is a hard error, not a fallback to PyPI: falling back is
# exactly how the two architectures would silently drift apart again.
RUN set -euo pipefail; \
    python -m venv "$VIRTUAL_ENV"; \
    pinned=(); \
    if [[ -f constraints.txt ]]; then \
        echo "Installing against the versions resolved once for this release:"; \
        cat constraints.txt; \
        pinned=(--constraint constraints.txt); \
    else \
        echo "No constraints.txt in the build context - resolving the newest set here"; \
    fi; \
    pip install --upgrade "${pinned[@]}" pip; \
    shopt -s nullglob; \
    essentia_wheels=(/tmp/essentia-wheels/essentia-*.whl); \
    (( ${#essentia_wheels[@]} > 0 )) || \
        { echo "no essentia wheel came from the essentia stage - it must build on every architecture" >&2; exit 1; }; \
    echo "Installing locally built essentia: ${essentia_wheels[*]}"; \
    pip install --no-cache-dir "${pinned[@]}" "${essentia_wheels[@]}"; \
    python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' \
        > /tmp/requirements.txt; \
    echo "essentia: keeping the locally built version, dropping its requirement"; \
    sed -ri '/^essentia([[:space:]]*[<>=!~].*)?$/d' /tmp/requirements.txt; \
    ! grep -qi '^essentia' /tmp/requirements.txt || \
        { echo "essentia requirement survived the drop - pip would overwrite the local build" >&2; exit 1; }; \
    cat /tmp/requirements.txt; \
    pip install --no-cache-dir --extra-index-url "$TORCH_CPU_INDEX" \
        "${pinned[@]}" -r /tmp/requirements.txt; \
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
    echo "essentia $(python -c 'from importlib import metadata; print(metadata.version("essentia"))')" && \
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
