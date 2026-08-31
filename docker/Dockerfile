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


# --------------------------------------------------------------------------- #
# Stage: essentia - source build, arm64 only
# --------------------------------------------------------------------------- #
# essentia carries the primary BPM path (app/bpm.py imports RhythmExtractor2013
# and MonoLoader), so requirements.txt lists it unconditionally - but upstream
# publishes manylinux wheels for x86_64 only. There has never been a
# linux/aarch64 wheel for any release, and the PyPI sdists stop at
# 2.1b6.dev234 (2020), long before Python 3.13; neither conda-forge nor Debian
# package it either. Building from source is therefore the only way essentia
# reaches arm64 at all.
#
# On amd64 this stage does nothing and leaves /wheels empty - the builder stage
# below then installs the upstream wheel from PyPI as before, so the amd64
# build pays nothing for any of this.
#
# The build follows upstream's own wheel recipe rather than reinventing one:
# pyproject.toml declares numpy for the isolated build env, setup.py first runs
# packaging/build_3rdparty_static_debian.sh (eigen, fftw3, lame, ffmpeg,
# libsamplerate, zlib, taglib, yaml, chromaprint - all static) and then
# configures waf with --build-static --static-dependencies. gaia and tensorflow
# are deliberately not requested: gaia alone would pull in a full Qt 4.8 build,
# and nothing fetchly calls needs either.
#
# Everything lands inside _essentia.so, so the runtime image needs no extra
# shared libraries and docker/apt-no-distro-media.pref stays intact - the
# static ffmpeg 7.1.1 essentia links for decoding is entirely separate from the
# BtbN ffmpeg/ffprobe binaries the application shells out to.
#
# ESSENTIA_REF follows master, in keeping with the rest of this file: the
# toolchain rolls, and a release picks up the current upstream. Note that
# tracking master is not a looser choice than tracking releases here - it is
# the same thing. essentia's git tags stopped at v2.1_beta5 in 2019, and every
# 2.1b6.dev* on PyPI is itself a snapshot of a master commit, so master IS the
# release channel for this project.
#
# Pin it to a commit (--build-arg ESSENTIA_REF=<sha>) only to reproduce or roll
# back a specific build.
#
# The ADD below exists for the same reason as the yt-dlp and deno cache keys
# further down: Docker compares only the instruction text when deciding whether
# a RUN is cached, so with a floating ref a plain RUN would keep serving the
# essentia that happened to be current on the first build, forever. The commit
# feed for ESSENTIA_REF is refetched every build and its content changes
# exactly when that ref moves, so the compile below reruns then and only then.
#
# One consequence worth knowing: the two architectures will not generally carry
# the same essentia build. arm64 compiles whatever ESSENTIA_REF points at,
# while amd64 takes the newest PyPI wheel built for cp313 - and upstream builds
# a wheel per interpreter, so that lags master (at the time of writing, amd64
# resolves 2.1b6.dev1389 while master is past dev1450). Both are "the newest
# each platform can get", which is the intent; they are just not the same
# commit. --build-arg ESSENTIA_REF=<sha> is how to force them together if a
# BPM discrepancy between architectures ever needs ruling out.
#
# Two details of the RUN below are easy to get wrong: the clone is blobless
# rather than shallow, because setup.py derives the version from
# `git describe --always --tags` and a --depth 1 clone carries no tags at all -
# its documented fallback would silently label the wheel 2.1b6.dev0. And
# MAKEFLAGS is set because the upstream 3rdparty scripts call plain `make`,
# which is the only way to parallelize them short of patching each script.
# nasm is in the apt list for the same reason the rest of the toolchain is:
# essentia's bundled ffmpeg refuses to configure without an assembler on x86.
# arm64 does not consume it - gcc assembles the NEON paths - but leaving it out
# would make this stage silently unbuildable the moment the arch guard is
# lifted, e.g. to reproduce an arm64 failure locally on an x86 machine.
#
# The FFTW_FLAGS line appended to packaging/build_config.sh below is what makes
# this stage build at all on aarch64. Upstream hardcodes an x86-only set there:
#
#     FFTW_FLAGS="--enable-float --enable-sse2
#                 --with-incoming-stack-boundary=2 --with-our-malloc16"
#
# and packaging/debian_3rdparty/build_fftw3.sh passes it straight to FFTW's
# configure. On aarch64 --enable-sse2 makes that configure probe for -msse and
# abort - "configure: error: Need a version of gcc with -msse" - before any of
# the remaining 3rdparty libraries or essentia itself is reached. The other two
# flags are x86 stack/alloc tuning and equally meaningless here.
#
# FFTW_VERSION is raised in the same breath because the pinned 3.3.2 (2012)
# predates aarch64 NEON support - on that version there would be no SIMD flag
# to swap in and the only option would be a scalar build. 3.3.10 covers
# aarch64 under --enable-neon, is API-compatible for the single-precision
# fftw3f that essentia and chromaprint link against, and is served from the
# same http://www.fftw.org/$FFTW_VERSION.tar.gz layout build_fftw3.sh already
# uses. --enable-neon requires --enable-float, which upstream sets anyway.
#
# Appending re-assigns rather than patching individual lines out, so an
# upstream reorder or reword of that block cannot silently un-fix this; the
# shell simply takes the last assignment. Drop both overrides once upstream
# ships a build_config.sh that is arch-aware on its own.
#
# CONFIG_SITE covers the second aarch64 blocker in that same chain. Several of
# the pinned 3rdparty tarballs are old enough to ship a config.guess that
# predates aarch64 - libsamplerate 0.1.9 carries one stamped 2009-04-27 - so
# their configure aborts with "cannot guess build type; you must specify one"
# on a machine whose uname -m is aarch64. Presetting the ac_cv_build and
# ac_cv_host cache variables makes AC_CANONICAL_BUILD skip config.guess and
# config.sub entirely, which fixes every autotools package in the chain at
# once rather than one --build flag per build_*.sh.
#
# Only autoconf reads CONFIG_SITE, which is exactly the wanted blast radius:
# ffmpeg's hand-written configure and the cmake-based builds (eigen, taglib,
# chromaprint) ignore the file, so the two that already configure correctly
# here are not touched.
FROM ${PYTHON_BASE} AS essentia

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH
ARG ESSENTIA_REPO=https://github.com/MTG/essentia.git
ARG ESSENTIA_REF=master

COPY docker/apt-no-distro-media.pref /etc/apt/preferences.d/no-distro-media

# The layer cache key for the compile below - see the note above. The Atom feed
# is used rather than the GitHub API because it carries no rate limit, which a
# shared CI egress address would otherwise run into.
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
    PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# Create venv and install Python dependencies.
COPY requirements.txt ./requirements.txt

# Empty on amd64, one essentia wheel on arm64 (see the essentia stage above).
COPY --from=essentia /wheels /tmp/essentia-wheels

# The locally built wheel has to go in before requirements.txt, not after:
# `essentia` is unpinned there, so an already-installed distribution satisfies
# it and pip never goes looking for the linux/aarch64 wheel upstream does not
# publish. On amd64 the glob matches nothing and pip resolves essentia from
# PyPI exactly as before.
RUN set -euo pipefail; \
    python -m venv "$VIRTUAL_ENV"; \
    pip install --upgrade pip; \
    shopt -s nullglob; \
    essentia_wheels=(/tmp/essentia-wheels/essentia-*.whl); \
    if (( ${#essentia_wheels[@]} > 0 )); then \
        echo "Installing locally built essentia: ${essentia_wheels[*]}"; \
        pip install --no-cache-dir "${essentia_wheels[@]}"; \
    fi; \
    pip install --no-cache-dir -r requirements.txt; \
    rm -rf /tmp/essentia-wheels

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

# Keep only runtime material in the venv copied into the final image. pip
# precompiles every installed module and several scientific packages ship
# sizeable test suites, C/C++ headers and test executables. None of those are
# used by Fetchly at runtime. In particular, torch/testing is deliberately
# retained: torch imports parts of it during normal operation.
#
# This cleanup has to happen in the builder stage before /venv is copied.
# Deleting the same files in the runtime stage would only add whiteouts and
# leave their bytes in the preceding image layer.
RUN set -euo pipefail; \
    site_packages="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    python -m pip check; \
    find "$VIRTUAL_ENV" -type d -name __pycache__ -prune -exec rm -rf '{}' +; \
    find "$site_packages" -depth -type d \( -name test -o -name tests \) -exec rm -rf '{}' +; \
    rm -rf "$site_packages/torch/include"; \
    find "$site_packages/torch/bin" -maxdepth 1 -type f \
        \( -name 'test*' -o -name '*Test' -o -name 'protoc*' \) -delete; \
    test -x "$site_packages/torch/bin/torch_shm_manager"

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
#
# `apt-get upgrade` is the point of this layer as much as the two installs are.
# `apt-get update` only refreshes the package index; every package inherited
# from python:3.13-slim that is never named in an install line stays at
# whatever version that base image was last rebuilt with. Docker Official
# Images lag Debian security uploads by days, which is how the release scan
# came to fail on openssl/libssl3t64 (CVE-2026-14456) even though the build
# pulls the newest python:3.13-slim tag. Upgrading here closes that window on
# every rebuild, matching the "security updates arrive with a rebuild" note at
# the top of this file. `upgrade` rather than `dist-upgrade`: it only upgrades
# packages in place and never installs or removes any. The ffmpeg/libav pins
# in apt-no-distro-media.pref are honoured by this too, so the distro media
# stack still cannot come back in through an upgrade.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
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
    PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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
