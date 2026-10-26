//
// app/static/js/events.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG, PROGRESS_STATUSES } from "./config.js";
import { fetchJob, fetchJobs } from "./api.js";
import { reportError, reportWarning } from "./errors.js";
// Specifier must be byte-identical to main.js's: a different query string is a
// different module, loading the job store twice with separate state.
import { applyJobUpdate, upsertJobSnapshot } from "./jobs.js";

export const EVENT_NAMES = Object.freeze({
    JOB_UPDATE: "fetchly:job-update",
    LALAL_PROGRESS: "fetchly:lalal-progress",
});

const queuedJobUpdates = new Map();
const latestSequences = new Map();

/** @type {number | null} */
let queuedJobUpdateFrame = null;

/**
 * @typedef {object} JobUpdatePayload
 * @property {string} id
 * @property {string} [type]
 * @property {string} [status]
 * @property {string} [message]
 * @property {string} [url]
 * @property {string} [video_title]
 * @property {string} [video_meta_hover]
 * @property {string} [codec]
 * @property {number} [bitrate_kbps]
 * @property {number} [bpm]
 * @property {number} [bpm_confidence]
 * @property {number} [filesize_bytes]
 * @property {number} [progress]
 * @property {number} [seq]
 */

/** @type {number | null} */
let reconnectTimer = null;

/** @type {EventSource | null} */
let activeStream = null;

/** @type {number} */
let reconnectAttempt = 0;
let shouldReconnect = true;
let streamEnabled = true;
const recoveryRequestsInFlight = new Set();
let reconciliationInFlight = false;
let reconciliationPending = false;
// Set when the stream was closed on purpose (hidden tab, pagehide) so the
// next open re-syncs from a snapshot even though no error was involved.
let needsReconciliation = false;
// Live-update clock: a snapshot entry older than the job's last live update is stale.
let jobUpdateEpoch = 0;
const liveUpdateEpochs = new Map();
const MAX_RECONCILE_PASSES = 3;

export function dispatchAppEvent(name, detail) {
    document.dispatchEvent(new CustomEvent(name, { detail }));
}

export function dispatchJobUpdate(detail) {
    dispatchAppEvent(EVENT_NAMES.JOB_UPDATE, detail);
}

function flushQueuedJobUpdates() {
    queuedJobUpdateFrame = null;

    const payloads = [...queuedJobUpdates.values()];
    queuedJobUpdates.clear();

    for (const payload of payloads) {
        const handled = applyUpdate(payload);
        if (!handled) {
            void recoverUnknownJobUpdate(payload);
        }
    }
}

function scheduleJobUpdate(payload) {
    if (!payload?.id) {
        reportWarning("SSE update missing job id", {
            module: "events",
            action: "scheduleJobUpdate",
            payload,
        });
        return;
    }

    if (!isNewerUpdate(payload)) {
        return;
    }

    const jobId = String(payload.id);
    jobUpdateEpoch += 1;
    liveUpdateEpochs.set(jobId, jobUpdateEpoch);
    if (liveUpdateEpochs.size > 1000) {
        liveUpdateEpochs.clear();
        liveUpdateEpochs.set(jobId, jobUpdateEpoch);
    }
    queuedJobUpdates.set(jobId, mergeQueuedUpdate(queuedJobUpdates.get(jobId), payload));
    if (queuedJobUpdateFrame !== null) {
        return;
    }

    queuedJobUpdateFrame = window.requestAnimationFrame(() => {
        flushQueuedJobUpdates();
    });
}

/**
 * Merge two updates of one job within a frame. A spread would keep an older
 * progress when the newer status carries none, which the store treats as "absent".
 */
function mergeQueuedUpdate(previous, payload) {
    const merged = { ...(previous || {}), ...payload };
    if (
        Object.hasOwn(payload, "status")
        && !PROGRESS_STATUSES.has(payload.status)
        && !Object.hasOwn(payload, "progress")
    ) {
        delete merged.progress;
    }
    return merged;
}

function isNewerUpdate(payload) {
    const seq = Number(payload?.seq);
    if (!Number.isFinite(seq)) {
        return true;
    }

    const id = String(payload.id);
    const previousSeq = latestSequences.get(id) || 0;
    if (seq <= previousSeq) {
        return false;
    }

    latestSequences.set(id, seq);
    if (latestSequences.size > 1000) {
        latestSequences.clear();
        latestSequences.set(id, seq);
    }
    return true;
}

function clearReconnectTimer() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

/** @param {EventSource | null} stream - closed and detached */
function teardown(stream) {
    clearReconnectTimer();

    if (!stream) {
        return;
    }

    if (activeStream === stream) {
        activeStream = null;
    }

    stream.onopen = null;
    stream.onmessage = null;
    stream.onerror = null;

    try {
        stream.close();
    } catch (err) {
        reportWarning("SSE close error", {
            module: "events",
            action: "teardown",
            error: err?.message || String(err),
        });
    }
}

/** Schedule a reconnect with exponential backoff + jitter. */
function scheduleReconnect() {
    if (!shouldReconnect || !streamEnabled) {
        return;
    }

    clearReconnectTimer();

    reconnectAttempt += 1;
    const baseDelay = Math.min(CONFIG.SSE_RECONNECT_MS * (2 ** (reconnectAttempt - 1)), 30_000);
    const jitter = Math.random() * Math.min(baseDelay * 0.5, 5_000);

    reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectEventStream();
    }, baseDelay + jitter);
}

/** @param {JobUpdatePayload} payload - applied to the job store, or null if unknown */
function applyUpdate(payload) {
    if (!payload?.id) {
        reportWarning("SSE update missing job id", {
            module: "events",
            action: "applyUpdate",
            payload,
        });
        return null;
    }

    const updatedJob = applyJobUpdate(payload);
    if (!updatedJob) {
        return null;
    }

    dispatchJobUpdate(updatedJob);
    return updatedJob;
}

async function recoverUnknownJobUpdate(payload) {
    if (!payload?.id) {
        return;
    }

    const jobId = String(payload.id);
    if (recoveryRequestsInFlight.has(jobId)) {
        return;
    }

    recoveryRequestsInFlight.add(jobId);

    try {
        const job = await fetchJob(jobId);
        const updatedJob = upsertJobSnapshot(job);
        if (!updatedJob) {
            return;
        }

        dispatchJobUpdate(updatedJob);
    } catch (error) {
        if (error?.status === 404) {
            return;
        }
        reportWarning("Failed to recover unknown SSE job", {
            module: "events",
            action: "recoverUnknownJobUpdate",
            jobId,
            error: error?.message || String(error),
        });
    } finally {
        recoveryRequestsInFlight.delete(jobId);
    }
}

async function reconcileVisibleJobs() {
    if (reconciliationInFlight) {
        reconciliationPending = true;
        return;
    }
    reconciliationInFlight = true;
    try {
        for (let pass = 0; pass < MAX_RECONCILE_PASSES; pass += 1) {
            reconciliationPending = false;
            const skipped = await reconcileOnce();
            if (!skipped && !reconciliationPending) {
                break;
            }
        }
    } catch (error) {
        reportWarning("Failed to reconcile jobs after SSE reconnect", {
            module: "events",
            action: "reconcileVisibleJobs",
            error: error?.message || String(error),
        });
    } finally {
        reconciliationInFlight = false;
        reconciliationPending = false;
    }
}

/**
 * Apply one server snapshot, skipping jobs a live update touched since the
 * request started (the snapshot is older for those).
 * @returns {Promise<boolean>} true when at least one entry was skipped
 */
async function reconcileOnce() {
    const startEpoch = jobUpdateEpoch;
    // no-store: never reuse a response (or in-flight request) older than this call.
    const jobs = await fetchJobs(0, { cache: "no-store" });
    if (!Array.isArray(jobs)) {
        return false;
    }

    let skipped = false;
    // Server page is newest-first. Known jobs are patched in place; unknown
    // ones are prepended oldest-first so the newest still lands on top.
    const unknownJobs = [];
    for (const job of jobs) {
        if ((liveUpdateEpochs.get(String(job?.id)) ?? 0) > startEpoch) {
            skipped = true;
            continue;
        }
        const updatedJob = applyJobUpdate(job);
        if (updatedJob) {
            dispatchJobUpdate(updatedJob);
            continue;
        }
        unknownJobs.push(job);
    }

    for (let index = unknownJobs.length - 1; index >= 0; index -= 1) {
        const insertedJob = upsertJobSnapshot(unknownJobs[index]);
        if (insertedJob) {
            dispatchJobUpdate(insertedJob);
        }
    }
    return skipped;
}

function resetEventSequenceState() {
    latestSequences.clear();
    queuedJobUpdates.clear();
    if (queuedJobUpdateFrame !== null) {
        window.cancelAnimationFrame(queuedJobUpdateFrame);
        queuedJobUpdateFrame = null;
    }
}

async function recoverStreamFailure(stream) {
    if (activeStream !== stream) {
        return;
    }

    teardown(stream);

    try {
        // EventSource hides the failing HTTP status; a normal API request
        // tells an expired session apart from a transient failure.
        await fetchJobs(0);
    } catch (error) {
        if (error?.status === 401 || error?.status === 403) {
            shouldReconnect = false;
            window.location.replace("/login");
            return;
        }
    }

    scheduleReconnect();
}

/** @param {string} data - raw SSE message; parsed and dispatched */
function handleMessage(data) {
    try {
        const payload = JSON.parse(data);

        // Server shutting down: close and reconnect later.
        if (payload.type === "shutdown") {
            shouldReconnect = true;
            if (activeStream) {
                teardown(activeStream);
            }
            scheduleReconnect();
            return;
        }

        if (payload.type === "authentication_required") {
            // Session gone server-side: stop reconnecting and tear down before
            // navigating so a late event cannot reopen the stream.
            shouldReconnect = false;
            if (activeStream) {
                teardown(activeStream);
            }
            window.location.replace("/login");
            return;
        }

        if (payload.type === "resync_required") {
            void reconcileVisibleJobs();
            return;
        }

        if (payload.type === "lalal_progress") {
            dispatchAppEvent(EVENT_NAMES.LALAL_PROGRESS, payload);
            return;
        }

        scheduleJobUpdate(payload);
    } catch (err) {
        reportError(err, {
            module: "events",
            action: "handleMessage",
            data,
        });
    }
}

/**
 * Establish (or reuse) the SSE connection for real-time job updates.
 * @returns {EventSource | null}
 */
export function connectEventStream() {
    if (!streamEnabled) {
        return null;
    }

    clearReconnectTimer();

    if (activeStream && activeStream.readyState !== EventSource.CLOSED) {
        return activeStream;
    }

    if (activeStream) {
        teardown(activeStream);
    }

    const stream = new EventSource("/events", { withCredentials: true });
    activeStream = stream;

    stream.onopen = () => {
        const shouldReconcile = needsReconciliation || reconnectAttempt > 0;
        needsReconciliation = false;
        reconnectAttempt = 0;
        if (shouldReconcile) {
            resetEventSequenceState();
            void reconcileVisibleJobs();
        }
    };

    stream.onmessage = (event) => {
        handleMessage(event.data);
    };

    stream.onerror = () => {
        void recoverStreamFailure(stream);
    };

    return stream;
}

/**
 * Enable/disable the dashboard SSE connection (disabling closes the stream and
 * suppresses reconnects).
 * @param {boolean} enabled
 */
export function setEventStreamEnabled(enabled) {
    streamEnabled = enabled;
    if (!enabled) {
        clearReconnectTimer();
        if (activeStream) {
            teardown(activeStream);
        }
        return;
    }

    if (shouldReconnect) {
        connectEventStream();
    }
}

function handlePageHide() {
    shouldReconnect = false;
    needsReconciliation = true;
    clearReconnectTimer();
    if (activeStream) {
        teardown(activeStream);
    }
}

function handlePageShow(event) {
    if (event.persisted) {
        shouldReconnect = true;
        if (streamEnabled) {
            connectEventStream();
        }
    }
}

function handleVisibilityChange() {
    if (document.visibilityState !== "visible") {
        // A pending reconnect timer would reopen the stream while hidden.
        clearReconnectTimer();
        needsReconciliation = true;
        if (activeStream) {
            teardown(activeStream);
        }
        return;
    }

    shouldReconnect = true;
    if (streamEnabled) {
        connectEventStream();
    }
}

window.addEventListener("pagehide", handlePageHide);
window.addEventListener("pageshow", handlePageShow);
document.addEventListener("visibilitychange", handleVisibilityChange);
