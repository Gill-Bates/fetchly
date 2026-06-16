//
// app/static/js/events.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";
import { fetchJob } from "./api.js";
import { reportError, reportWarning } from "./errors.js";
import { applyJobUpdate, hasNonTerminalJobs, upsertJobSnapshot } from "./jobs.js";

export const EVENT_NAMES = Object.freeze({
    JOB_UPDATE: "tubeyou:job-update",
    LALAL_PROGRESS: "tubeyou:lalal-progress",
});

const INDICATOR_STATE_CLASSES = Object.freeze(["bg-success", "bg-secondary", "bg-danger", "live", "text-dark"]);
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
    queuedJobUpdates.set(jobId, {
        ...(queuedJobUpdates.get(jobId) || {}),
        ...payload,
    });
    if (queuedJobUpdateFrame !== null) {
        return;
    }

    queuedJobUpdateFrame = window.requestAnimationFrame(() => {
        flushQueuedJobUpdates();
    });
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

/**
 * Update the connection indicator badge.
 * @param {HTMLElement | null} indicator - The indicator element
 * @param {"processing" | "idle" | "offline"} state - Desired indicator state
 */
function setIndicator(indicator, state) {
    if (!indicator) return;

    if (indicator.dataset.wsState === state) {
        return;
    }

    indicator.classList.remove(...INDICATOR_STATE_CLASSES);
    indicator.classList.add("ws-indicator", "badge");

    switch (state) {
        case "processing":
            indicator.textContent = "● Processing";
            indicator.classList.add("bg-success", "live");
            break;
        case "offline":
            indicator.textContent = "● Reconnecting";
            indicator.classList.add("bg-danger");
            break;
        default:
            indicator.textContent = "● Idle";
            indicator.classList.add("bg-secondary");
            state = "idle";
            break;
    }
    indicator.dataset.wsState = state;
}

function syncIndicatorState(indicator = document.getElementById("wsIndicator")) {
    setIndicator(indicator, hasNonTerminalJobs() ? "processing" : "idle");
}

/**
 * Tear down an EventSource cleanly.
 * @param {EventSource | null} stream - The stream to tear down
 */
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

/**
 * Schedule a reconnect attempt using exponential backoff and jitter.
 */
function scheduleReconnect() {
    if (!shouldReconnect || !streamEnabled) {
        return;
    }

    clearReconnectTimer();
    setIndicator(document.getElementById("wsIndicator"), "offline");

    reconnectAttempt += 1;
    const baseDelay = Math.min(CONFIG.WS_RECONNECT_MS * (2 ** (reconnectAttempt - 1)), 30_000);
    const jitter = Math.random() * Math.min(baseDelay * 0.5, 5_000);

    reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectEventStream();
    }, baseDelay + jitter);
}

/**
 * Apply a job status update to the DOM.
 * @param {JobUpdatePayload} payload - The job update payload from the server
 */
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

    syncIndicatorState();

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

        syncIndicatorState();
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

/**
 * Handle incoming SSE message and process job updates.
 * @param {string} data - The raw message data
 */
function handleMessage(data) {
    try {
        const payload = JSON.parse(data);

        // Server shutdown signal - close connection gracefully
        if (payload.type === "shutdown") {
            shouldReconnect = false;
            if (activeStream) {
                teardown(activeStream);
            }
            return;
        }

        // Handle Lalal.ai progress updates
        if (payload.type === "lalal_progress") {
            dispatchAppEvent(EVENT_NAMES.LALAL_PROGRESS, payload);
            return;
        }

        // Regular job update
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
 * Establish an SSE connection for real-time job updates.
 * Reuses the active stream when it is already open or connecting.
 * @returns {EventSource | null} The active or newly created event stream
 */
export function connectEventStream() {
    if (!streamEnabled) {
        return null;
    }

    clearReconnectTimer();

    const indicator = document.getElementById("wsIndicator");
    syncIndicatorState(indicator);

    if (activeStream && activeStream.readyState !== EventSource.CLOSED) {
        return activeStream;
    }

    if (activeStream) {
        teardown(activeStream);
    }

    const stream = new EventSource("/events", { withCredentials: true });
    activeStream = stream;

    stream.onopen = () => {
        reconnectAttempt = 0;
        syncIndicatorState(indicator);
    };

    stream.onmessage = (event) => {
        handleMessage(event.data);
    };

    stream.onerror = () => {
        syncIndicatorState(indicator);
        if (activeStream !== stream) {
            return;
        }

        teardown(stream);
        scheduleReconnect();
    };

    return stream;
}

/**
 * Enable or disable the dashboard SSE connection.
 * When disabled, any active stream is closed and reconnects are suppressed.
 * @param {boolean} enabled
 */
export function setEventStreamEnabled(enabled) {
    streamEnabled = enabled;
    if (!enabled) {
        clearReconnectTimer();
        if (activeStream) {
            teardown(activeStream);
        }
        setIndicator(document.getElementById("wsIndicator"), "idle");
        return;
    }

    syncIndicatorState();
    if (shouldReconnect) {
        connectEventStream();
    }
}

/**
 * Tear down the active SSE stream when the page is being hidden or unloaded.
 */
function handlePageHide() {
    shouldReconnect = false;
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