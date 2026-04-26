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
readonly UVICORN_HOST="${UVICORN_HOST:-0.0.0.0}"
readonly UVICORN_PORT="${UVICORN_PORT:-8000}"
readonly UVICORN_WORKERS="${UVICORN_WORKERS:-1}"
readonly MAX_RESTARTS="${MAX_RESTARTS:-5}"
readonly RESTART_DELAY="${RESTART_DELAY:-3}"

# --------------------------------------------------------------------------- #
# DRY: Zentrales Logging
# --------------------------------------------------------------------------- #
_log() {
    local tag="$1"
    shift
    printf '[%s] %s\n' "$tag" "$*"
}

# --------------------------------------------------------------------------- #
# 1. Bootstrap
# --------------------------------------------------------------------------- #
_log entrypoint "Bootstrap: Erstelle Verzeichnisse unter ${DATA_DIR} ..."

mkdir -p \
    "${DATA_DIR}/downloads" \
    "${DATA_DIR}/logs"

# WARNUNG: 'chown -R' / 'chmod -R' auf großen Datenbeständen blockiert
# den Container-Start. Nur Top-Level-Verzeichnis anwenden.
chown "$(id -u):$(id -g)" "${DATA_DIR}" 2>/dev/null || true
chmod u=rwX,go-rwx "${DATA_DIR}" 2>/dev/null || true

_log entrypoint "Bootstrap abgeschlossen."

# Sicherheitshalber: Existiert uvicorn?
if ! command -v uvicorn >/dev/null 2>&1; then
    _log entrypoint "FEHLER: uvicorn nicht im PATH."
    exit 1
fi

# --------------------------------------------------------------------------- #
# 2. Watchdog (mit korrektem Signal-Handling)
# --------------------------------------------------------------------------- #
_run_watchdog() {
    local -i shutdown_requested=0
    local -i restarts=0
    local child_pid=""
    local wait_exit=0

    # Signal-Handler: Setzt Flag, killt Kind, aber wartet NICHT im Trap.
    # Das Warten geschieht ausschließlich im Hauptflow.
    _on_signal() {
        local sig="$1"
        _log watchdog "Signal ${sig} empfangen – initiiere Shutdown ..."
        shutdown_requested=1
        if [[ -n "${child_pid:-}" ]] && kill -0 "${child_pid}" 2>/dev/null; then
            kill -TERM "${child_pid}" 2>/dev/null || true
        fi
    }

    trap '_on_signal TERM' SIGTERM
    trap '_on_signal INT'  SIGINT

    _log watchdog "Starte uvicorn app.main:app auf ${UVICORN_HOST}:${UVICORN_PORT} ..."

    while (( shutdown_requested == 0 )); do
        # shellcheck disable=SC2086
        uvicorn app.main:app \
            --host "${UVICORN_HOST}" \
            --port "${UVICORN_PORT}" \
            --workers "${UVICORN_WORKERS}" \
            --no-access-log \
            &
        child_pid=$!

        # Warte auf Kind. '||' verhindert, dass set -e bei Exit != 0 abbricht.
        wait_exit=0
        wait "${child_pid}" || wait_exit=$?

        # KRITISCH: Wenn ein Signal den Shutdown angefordert hat → sofort beenden,
        # unabhängig vom Exit-Code des Kindes.
        if (( shutdown_requested )); then
            # Kind nochmal kurz abwarten (reaped oder nicht)
            wait "${child_pid}" 2>/dev/null || true
            _log watchdog "Sauber beendet."
            return 0
        fi

        # Sauberes Beenden → Watchdog beenden
        if (( wait_exit == 0 )); then
            _log watchdog "uvicorn sauber beendet (exit 0)."
            return 0
        fi

        restarts=$(( restarts + 1 ))
        _log watchdog "uvicorn beendet mit exit ${wait_exit} (Neustart ${restarts}/${MAX_RESTARTS})"

        if (( restarts >= MAX_RESTARTS )); then
            _log watchdog "FEHLER: Maximale Neustarts (${MAX_RESTARTS}) erreicht."
            return 1
        fi

        # Wenn sleep durch Signal unterbrochen wird, bricht es mit != 0 ab.
        # Die while-Bedingung verhindert danach den Neustart.
        sleep "${RESTART_DELAY}" || true

        # Reset für den nächsten Durchlauf
        child_pid=""
    done

    return 0
}

_run_watchdog
