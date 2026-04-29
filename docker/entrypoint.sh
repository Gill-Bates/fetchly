#!/usr/bin/env bash
#
# docker/entrypoint.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

set -euo pipefail

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
readonly DATA_DIR="${DATA_DIR:-/app/data}"
readonly APP_USER="${APP_USER:-appuser}"
readonly HOST="${HOST:-${UVICORN_HOST:-0.0.0.0}}"
readonly PORT="${PORT:-${UVICORN_PORT:-8000}}"
readonly WORKERS="${WORKERS:-${UVICORN_WORKERS:-auto}}"
readonly TIMEOUT="${TIMEOUT:-60}"
readonly MAX_WORKERS="${MAX_WORKERS:-8}"
readonly LOG_LEVEL="$(printf '%s' "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
readonly FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7}"
readonly ACCESS_LOG_FORMAT="${ACCESS_LOG_FORMAT:-[%(t)s] %(h)s \"%(r)s\" %(s)s %(b)s}"

export FORWARDED_ALLOW_IPS

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
log() {
    printf '[%s] [%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "entrypoint" "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

resolve_workers() {
    if [[ "${WORKERS}" != "auto" ]]; then
        if ! [[ "${WORKERS}" =~ ^[0-9]+$ ]]; then
            fail "WORKERS must be numeric or 'auto', got: ${WORKERS}"
        fi
        printf '%s\n' "${WORKERS}"
        return 0
    fi

    python - <<'PY'
import math
import os
from pathlib import Path


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _parse_cpuset(spec: str | None) -> int | None:
    if not spec:
        return None
    total = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            total += int(end) - int(start) + 1
        else:
            total += 1
    return total or None


limits: list[float] = []

try:
    limits.append(float(len(os.sched_getaffinity(0))))
except (AttributeError, OSError):
    pass

cpuset = _parse_cpuset(_read_text("/sys/fs/cgroup/cpuset.cpus.effective"))
if cpuset is None:
    cpuset = _parse_cpuset(_read_text("/sys/fs/cgroup/cpuset/cpuset.cpus"))
if cpuset is not None:
    limits.append(float(cpuset))

cpu_max = _read_text("/sys/fs/cgroup/cpu.max")
if cpu_max:
    parts = cpu_max.split()
    if len(parts) >= 2:
        quota, period = parts[:2]
        if quota != "max":
            try:
                limits.append(float(quota) / float(period))
            except (ValueError, ZeroDivisionError):
                pass
else:
    quota = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period:
        quota_value = int(quota)
        period_value = int(period)
        if quota_value > 0 and period_value > 0:
            limits.append(quota_value / period_value)

if not limits:
    fallback = os.cpu_count() or 1
    limits.append(float(fallback))

effective_cpus = max(0.5, min(limits))
workers = max(1, math.ceil(effective_cpus * 2.0))

# safety clamp
max_workers = int(os.getenv("MAX_WORKERS", "8"))
workers = min(workers, max_workers)

print(workers if workers > 0 else 1)
PY
}

bootstrap() {
    log "Initializing ${DATA_DIR} ..."

    mkdir -p \
        "${DATA_DIR}/downloads"

    if [[ "$(id -u)" -eq 0 ]]; then
        if ! id "${APP_USER}" >/dev/null 2>&1; then
            fail "User ${APP_USER} does not exist"
        fi

        # only fix ownership if needed (avoid expensive recursive chown)
        if [[ "$(stat -c %u "${DATA_DIR}")" != "$(id -u "${APP_USER}")" ]]; then
            chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}" 2>/dev/null || true
        fi
    fi

    chmod u=rwX,go-rwx \
        "${DATA_DIR}" \
        "${DATA_DIR}/downloads" 2>/dev/null || true

    if [[ ! -w "${DATA_DIR}" ]] || [[ ! -w "${DATA_DIR}/downloads" ]]; then
        log "ERROR: ${DATA_DIR} not writable for user $(id -u):$(id -g)"
        ls -ld "${DATA_DIR}" "${DATA_DIR}/downloads" 2>/dev/null || true
        exit 1
    fi

    log "Bootstrap complete."
}

# --------------------------------------------------------------------------- #
# 1. Bootstrap
# --------------------------------------------------------------------------- #
if [[ "$(id -u)" -eq 0 ]] && [[ "${1:-}" != "--run" ]]; then
    bootstrap
    log "Switching to user ${APP_USER} ..."

    if ! command -v gosu >/dev/null 2>&1; then
        fail "gosu not found in PATH"
    fi

    exec gosu "${APP_USER}" /entrypoint.sh --run
fi

bootstrap

# --------------------------------------------------------------------------- #
# 2. Start Gunicorn
# --------------------------------------------------------------------------- #
if ! command -v gunicorn >/dev/null 2>&1; then
    fail "gunicorn not found in PATH"
fi

resolved_workers="$(resolve_workers)"

if ! [[ "${resolved_workers}" =~ ^[0-9]+$ ]]; then
    fail "Resolved workers is not numeric: ${resolved_workers}"
fi

# Print startup banner
python -c "from app.utils.banner import print_banner; print_banner()" 2>/dev/null || true

log "Starting Gunicorn on ${HOST}:${PORT} with ${resolved_workers} workers ..."

exec gunicorn app.main:app \
    --bind "${HOST}:${PORT}" \
    --workers "${resolved_workers}" \
    --worker-class uvicorn.workers.UvicornWorker \
    --log-level "${LOG_LEVEL}" \
    --timeout "${TIMEOUT}" \
    --access-logfile - \
    --access-logformat "${ACCESS_LOG_FORMAT}" \
    --error-logfile -
