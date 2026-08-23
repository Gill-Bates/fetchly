#!/usr/bin/env bash
#
# docker/entrypoint.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

set -euo pipefail

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
readonly DATA_DIR="${DATA_DIR:-/app/data}"
# beat-this resolves checkpoint "final0" through torch.hub and downloads an
# 81 MB .ckpt from cloud.cp.jku.at on the first BPM analysis - the one runtime
# dependency the image does not bake in. torch's default cache is $HOME/.cache,
# which lives in the container's writable layer and is lost on every recreate,
# re-downloading those 81 MB each time. Pin it onto DATA_DIR so it persists
# with the volume. Honoured if the operator already set TORCH_HOME - in which
# case bootstrap() still has to create and chown it, since it is then outside
# the tree DATA_DIR's own ownership handling covers.
readonly TORCH_HOME="${TORCH_HOME:-${DATA_DIR}/.cache/torch}"
readonly APP_USER="${APP_USER:-appuser}"
readonly HOST="${HOST:-${UVICORN_HOST:-0.0.0.0}}"
readonly PORT="${PORT:-${UVICORN_PORT:-8000}}"
# Fixed at 1, and enforced below rather than merely defaulted: the job queue
# and the SSE subscriber registry live in process memory with no cross-process
# coordination (app/worker.py, app/main.py). A second Gunicorn worker means the
# same job processed twice and clients subscribed to a process that never sees
# their job's events - a correctness failure, not a throughput trade-off, so
# there is no supported value other than 1 until an external queue and pub/sub
# exist. CPU parallelism comes from the Governor's semaphores (app/governor.py)
# and is unaffected.
readonly WORKERS="${WORKERS:-${UVICORN_WORKERS:-1}}"
readonly TIMEOUT="${TIMEOUT:-60}"
readonly LOG_LEVEL="$(printf '%s' "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
# Loopback only, matching Gunicorn's own default. A wider default would let any
# workload that can reach this container forge X-Forwarded-For and thereby
# choose its own rate-limit bucket (app/common/rate_limit.py keys on client IP,
# which also gates the login endpoint). Operators terminating TLS on another
# host must name that proxy explicitly, e.g.
#   FORWARDED_ALLOW_IPS=10.30.0.1
readonly FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,::1}"
readonly ACCESS_LOG_FORMAT="${ACCESS_LOG_FORMAT:-[%(t)s] %(h)s \"%(r)s\" %(s)s %(b)s}"

# Directories the application must be able to write after the privilege drop.
# Handled explicitly in bootstrap(): TORCH_HOME (and its parent) are created
# fresh on an upgrade of an existing volume, where DATA_DIR already carries the
# right owner and the recursive chown below therefore never fires.
readonly -a REQUIRED_DIRS=(
    "${DATA_DIR}"
    "${DATA_DIR}/downloads"
    "$(dirname "${TORCH_HOME}")"
    "${TORCH_HOME}"
)

export FORWARDED_ALLOW_IPS
export TORCH_HOME

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
log() {
    # stderr: log()/fail() output must never land on a caller's stdout.
    printf '[%s] [%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "entrypoint" "$*" >&2
}

fail() {
    log "ERROR: $*"
    exit 1
}

validate_positive_int() {
    local name="$1"
    local value="$2"

    if ! [[ "${value}" =~ ^[0-9]+$ ]] || [[ "${value}" -lt 1 ]]; then
        fail "${name} must be a positive integer, got: ${value}"
    fi
}

# DATA_DIR is operator-supplied and is handed to a recursive chown that runs as
# root, so a typo like DATA_DIR=/ or DATA_DIR=/app would rewrite ownership
# across the container - and across the host for a bind mount. Reject anything
# that is not an absolute path below a system directory before that can happen.
validate_data_dir() {
    [[ "${DATA_DIR}" == /* ]] || fail "DATA_DIR must be an absolute path, got: ${DATA_DIR}"

    local resolved
    resolved="$(realpath -m -- "${DATA_DIR}")" || fail "cannot resolve DATA_DIR: ${DATA_DIR}"

    case "${resolved}" in
        /|/app|/bin|/boot|/dev|/etc|/home|/lib|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/venv)
            fail "refusing to use ${resolved} as DATA_DIR: recursive chown would damage the system"
            ;;
    esac
}

validate_config() {
    validate_data_dir

    validate_positive_int "PORT" "${PORT}"
    (( PORT <= 65535 )) || fail "PORT must be between 1 and 65535, got: ${PORT}"

    validate_positive_int "TIMEOUT" "${TIMEOUT}"
    validate_positive_int "WORKERS" "${WORKERS}"

    # See the WORKERS comment above: any other value is an incorrect operating
    # mode, so fail loudly at start instead of corrupting job state later.
    (( WORKERS == 1 )) || fail \
        "WORKERS must be 1 (got ${WORKERS}): the job queue and SSE subscribers are process-local"

    # Gunicorn's own accepted set (--log-level); it does not include "trace".
    case "${LOG_LEVEL}" in
        critical|error|warning|info|debug) ;;
        *) fail "invalid LOG_LEVEL: ${LOG_LEVEL}" ;;
    esac
}

bootstrap() {
    log "Initializing ${DATA_DIR} ..."

    local dir
    for dir in "${REQUIRED_DIRS[@]}"; do
        mkdir -p -- "${dir}" || fail "cannot create ${dir}"
    done

    if [[ "$(id -u)" -eq 0 ]]; then
        if ! id "${APP_USER}" >/dev/null 2>&1; then
            fail "User ${APP_USER} does not exist"
        fi

        local app_uid
        app_uid="$(id -u "${APP_USER}")"

        # Recursive pass first: cheapest way to cover a fresh volume in one
        # shot. Skipped once DATA_DIR and downloads already carry the right
        # owner, so an existing, possibly large downloads tree is not walked
        # on every restart.
        if [[ "$(stat -c %u "${DATA_DIR}")" != "${app_uid}" ]] || [[ "$(stat -c %u "${DATA_DIR}/downloads")" != "${app_uid}" ]]; then
            local chown_err
            if ! chown_err="$(chown -R -- "${APP_USER}:${APP_USER}" "${DATA_DIR}" 2>&1)"; then
                log "WARNING: chown of ${DATA_DIR} failed: ${chown_err}"
            fi
        fi

        # Non-recursive per-directory pass: catches any REQUIRED_DIRS entry
        # that appeared after the recursive pass above already ran on an older
        # volume (e.g. TORCH_HOME's cache directory showing up for the first
        # time), and any entry that sits outside DATA_DIR entirely (an
        # operator-set TORCH_HOME).
        for dir in "${REQUIRED_DIRS[@]}"; do
            if [[ "$(stat -c %u "${dir}")" != "${app_uid}" ]]; then
                chown -- "${APP_USER}:${APP_USER}" "${dir}" 2>/dev/null \
                    || log "WARNING: chown of ${dir} failed"
            fi
        done
    fi

    chmod u=rwX,go-rwx -- "${REQUIRED_DIRS[@]}" 2>/dev/null || true

    for dir in "${REQUIRED_DIRS[@]}"; do
        if [[ ! -w "${dir}" ]]; then
            ls -ld -- "${dir}" 2>/dev/null || true
            fail "${dir} is not writable by $(id -u):$(id -g)"
        fi
    done

    log "Bootstrap complete."
}

# Validated before anything else runs, in both the root branch and the
# already-non-root branch below - a bad DATA_DIR or WORKERS value must not
# get as far as a recursive chown or a started server.
validate_config

# --------------------------------------------------------------------------- #
# 1. Bootstrap
# --------------------------------------------------------------------------- #
# Gated on UID alone. It used to also require "$1 != --run", but argv is
# caller-controlled - `docker run <image> --run` satisfied that check as
# root and skipped straight to Gunicorn without ever dropping to appuser.
# After gosu drops privileges below, UID is genuinely non-zero, so this
# block naturally only runs once regardless of what "--run" appears in argv.
if [[ "$(id -u)" -eq 0 ]]; then
    bootstrap

    if ! command -v gosu >/dev/null 2>&1; then
        fail "gosu not found in PATH"
    fi

    log "Switching to user ${APP_USER} ..."

    if [[ "$#" -gt 0 ]]; then
        exec gosu "${APP_USER}" "$@"
    fi

    exec gosu "${APP_USER}" "$0" --run
fi

bootstrap

if [[ "${1:-}" == "--run" ]]; then
    shift
fi

# Escape hatch for e.g. `docker run <image> bash`: run the given command as
# appuser instead of continuing to the Gunicorn startup below.
if [[ "$#" -gt 0 ]]; then
    exec "$@"
fi

# --------------------------------------------------------------------------- #
# 2. Start Gunicorn
# --------------------------------------------------------------------------- #
if ! command -v gunicorn >/dev/null 2>&1; then
    fail "gunicorn not found in PATH"
fi

# Print startup banner
python -c "from app.utils.banner import print_banner; print_banner()" 2>/dev/null || true

log "Starting Gunicorn on ${HOST}:${PORT} with ${WORKERS} worker ..."

exec gunicorn app.main:app \
    --bind "${HOST}:${PORT}" \
    --workers "${WORKERS}" \
    --worker-class uvicorn_worker.UvicornWorker \
    --log-level "${LOG_LEVEL}" \
    --timeout "${TIMEOUT}" \
    --access-logfile - \
    --access-logformat "${ACCESS_LOG_FORMAT}" \
    --error-logfile -
