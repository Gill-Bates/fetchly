//
// app/static/js/config.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export const CONFIG = Object.freeze({
    MAX_ROWS: 120,
    PAGE_SIZE: 50,
    SCROLL_OFFSET: 250,
    SSE_RECONNECT_MS: 3000,
});

export const AUDIO_TYPE = "audio";

// Server-owned Lalal.ai processing limit, rendered by base.html.
export const LALAL_MAX_DURATION_SECONDS = Number(
    document.documentElement?.dataset.lalalMaxDurationSeconds,
);
if (!Number.isFinite(LALAL_MAX_DURATION_SECONDS) || LALAL_MAX_DURATION_SECONDS <= 0) {
    throw new Error("Invalid Lalal.ai duration limit in page bootstrap data");
}
export const LALAL_MAX_DURATION_MINUTES = LALAL_MAX_DURATION_SECONDS / 60;

// The job-status contract is owned by the backend (app/db.py groups and
// app/utils/template_filters.py labels/colors) and serialized into page
// bootstrap data by base.html. This module is the single JS-side source the
// other modules import from; none of them keep their own status tables.
function parseStatusContract() {
    const raw = document.documentElement?.dataset.statusContract;
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch {
        throw new Error("Missing or invalid job-status contract in page bootstrap data");
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Missing or invalid job-status contract in page bootstrap data");
    }
    return parsed;
}

function statusSet(contract, key) {
    const values = contract[key];
    if (!Array.isArray(values)) {
        throw new Error(`Job-status contract is missing the "${key}" group`);
    }
    return new Set(values);
}

function statusMeta(contract) {
    const meta = contract.meta;
    if (!meta || typeof meta !== "object") {
        throw new Error("Job-status contract is missing the display metadata");
    }
    const frozen = {};
    for (const [status, entry] of Object.entries(meta)) {
        frozen[status] = Object.freeze({ color: entry.color, label: entry.label });
    }
    return Object.freeze(frozen);
}

const STATUS_CONTRACT = parseStatusContract();

export const DOWNLOADABLE_STATUSES = statusSet(STATUS_CONTRACT, "downloadable");

export const CANCELLABLE_STATUSES = statusSet(STATUS_CONTRACT, "cancellable");

// Re-queueable from scratch via POST /api/jobs/{id}/retry.
export const RETRYABLE_STATUSES = statusSet(STATUS_CONTRACT, "retryable");

export const TERMINAL_STATUSES = statusSet(STATUS_CONTRACT, "terminal");
// Statuses whose events carry a progress percentage; any other status drops it.
export const PROGRESS_STATUSES = statusSet(STATUS_CONTRACT, "progress");

// status -> { label, color }; the server derives it from status_label() and
// status_class() in app/utils/template_filters.py.
export const STATUS_META = statusMeta(STATUS_CONTRACT);
