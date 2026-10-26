#!/usr/bin/env bash
# Purpose: Build a CPU-only essentia wheel from the current upstream master.
# Usage:   build-essentia.sh [OUTPUT_DIR]   (default /wheels; TARGETARCH=amd64|arm64)
# Env:     ESSENTIA_REF  full commit SHA; CI sets it once per release so every
#          architecture builds the same revision. Unset: resolve master now.
# Output:  one essentia-*.whl in OUTPUT_DIR; the commit and the checks are printed.
#
# Fetchly needs MonoLoader and RhythmExtractor2013 only, so just FFTW, an
# audio-only static ffmpeg, libsamplerate and Eigen are built. Upstream's
# build_3rdparty_static_debian.sh is not run: it also builds taglib, yaml and
# chromaprint, and since 33108c70 onnxruntime with CUDA. Missing libraries make
# waf drop the dependent algorithms (see check_cfg in src/wscript).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly REPO_URL="${ESSENTIA_REPO:-https://github.com/MTG/essentia.git}"
readonly SRC_DIR="${ESSENTIA_SRC:-/src/essentia}"
readonly FFTW_VERSION_OVERRIDE="fftw-3.3.10"
readonly VERIFY_VENV="${ESSENTIA_VERIFY_VENV:-/tmp/essentia-verify}"

die() {
    echo "build-essentia: $*" >&2
    exit 1
}

resolve_ref() {
    local ref="${ESSENTIA_REF:-}"
    if [[ -z "${ref}" ]]; then
        ref="$(git ls-remote "${REPO_URL}" refs/heads/master | cut -f1)"
    fi
    [[ "${ref}" =~ ^[0-9a-f]{40}$ ]] || die "could not resolve a 40-hex essentia commit, got '${ref}'"
    printf '%s' "${ref}"
}

fetch_source() {
    local ref="$1" head
    rm -rf "${SRC_DIR}"
    git clone --quiet --filter=blob:none "${REPO_URL}" "${SRC_DIR}"
    git -C "${SRC_DIR}" checkout --quiet --detach "${ref}"
    head="$(git -C "${SRC_DIR}" rev-parse HEAD)"
    [[ "${head}" == "${ref}" ]] || die "checked out ${head}, expected ${ref}"
    echo "essentia commit: ${head} ($(git -C "${SRC_DIR}" describe --always --tags))"
}

build_static_deps() {
    local simd
    case "${TARGETARCH:?TARGETARCH must be amd64 or arm64}" in
        amd64) simd=--enable-sse2 ;;
        arm64) simd=--enable-neon ;;
        *) die "unsupported TARGETARCH: ${TARGETARCH}" ;;
    esac
    cd "${SRC_DIR}/packaging/debian_3rdparty"
    # build_config.sh is sourced by every script below; upstream's FFTW 3.3.2
    # predates aarch64, so both arches use 3.3.10 and differ only in the SIMD flag.
    printf '\nFFTW_VERSION=%s\nFFTW_FLAGS="%s %s"\n' \
        "${FFTW_VERSION_OVERRIDE}" --enable-float "${simd}" >> ../build_config.sh
    if [[ "${TARGETARCH}" == "arm64" ]]; then
        # FFTW's bundled config.guess does not know this aarch64 host.
        printf 'ac_cv_build=aarch64-unknown-linux-gnu\nac_cv_host=aarch64-unknown-linux-gnu\n' > /tmp/config.site
        export CONFIG_SITE=/tmp/config.site
    fi
    MAKEFLAGS="-j$(nproc)"
    export MAKEFLAGS
    ./build_eigen3.sh
    ./build_fftw3.sh
    ./build_ffmpeg.sh --no-muxers
    ./build_libsamplerate.sh
}

build_wheel() {
    local out="$1"
    mkdir -p "${out}"
    cd "${SRC_DIR}"
    MAKEFLAGS="-j$(nproc)"
    export MAKEFLAGS
    ESSENTIA_WHEEL_SKIP_3RDPARTY=1 pip wheel --no-cache-dir --no-deps -w "${out}" .
    shopt -s nullglob
    local built=("${out}"/essentia-*.whl)
    (( ${#built[@]} == 1 )) || die "expected exactly one essentia wheel, got: ${built[*]:-none}"
    git -C "${SRC_DIR}" rev-parse HEAD > "${out}/essentia.commit"
    echo "built $(basename "${built[0]}") from $(<"${out}/essentia.commit")"
}

verify_wheel() {
    local out="$1" venv="${VERIFY_VENV}"
    rm -rf "${venv}"
    python -m venv "${venv}"
    # setup.py declares no runtime requirements, but essentia/common.py imports both.
    "${venv}/bin/pip" install --quiet --no-cache-dir numpy six
    "${venv}/bin/pip" install --quiet --no-cache-dir --no-deps "${out}"/essentia-*.whl
    "${venv}/bin/python" "${SCRIPT_DIR}/essentia_check.py"
    rm -rf "${venv}"
}

main() {
    local out ref
    out="$(realpath -m "${1:-/wheels}")"
    ref="$(resolve_ref)"
    echo "essentia ref: ${ref} [${TARGETARCH:-unset}]"
    fetch_source "${ref}"
    build_static_deps
    build_wheel "${out}"
    verify_wheel "${out}"
    echo "essentia ${ref} built and verified"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
