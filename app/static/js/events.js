//
// app/static/js/events.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG, TERMINAL_STATUSES } from "./config.js";
import { createStatusElement, createActionButton, getActionButtonCategory } from "./ui.js?v=20260429m";

const DATA_LABELS = Object.freeze({
    TITLE: "Title",
    FORMAT: "Format",
    BITRATE: "Bitrate",
    BPM: "BPM",
    STATUS: "Status",
    ACTION: "Action",
});

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
 */

/** @type {number | null} */
let reconnectTimer = null;

/** @type {EventSource | null} */
let activeStream = null;

/** @type {number} */
let reconnectAttempt = 0;
let shouldReconnect = true;
let streamEnabled = true;

function clearReconnectTimer() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

/**
 * Update the connection indicator badge.
 * @param {HTMLElement | null} indicator - The indicator element
 * @param {boolean} online - Whether the connection is active
 */
function setIndicator(indicator, online) {
    if (!indicator) return;

    const nextState = online ? "live" : "offline";
    if (indicator.dataset.wsState === nextState) {
        return;
    }

    if (online) {
        indicator.textContent = "● LIVE";
        indicator.className = "ws-indicator badge bg-success live";
    } else {
        indicator.textContent = "● offline";
        indicator.className = "ws-indicator badge bg-danger";
    }
    indicator.dataset.wsState = nextState;
}

/**
 * Find a table cell by data-label attribute.
 * @param {HTMLElement} row - The table row
 * @param {string} label - The data-label value to find
 * @returns {HTMLElement | null}
 */
function getCell(row, label) {
    return row.querySelector(`td[data-label="${CSS.escape(String(label))}"]`);
}

function getRenderedActionCategory(root) {
    return root?.querySelector("[data-action-category]")?.dataset.actionCategory || null;
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
        console.warn("SSE close error:", err);
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
    setIndicator(document.getElementById("wsIndicator"), false);

    reconnectAttempt += 1;
    const baseDelay = Math.min(CONFIG.WS_RECONNECT_MS * (2 ** (reconnectAttempt - 1)), 30_000);
    const jitter = Math.random() * 1000;

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
        console.warn("SSE update missing job id:", payload);
        return;
    }

    const row = document.querySelector(`tr[data-job-id="${CSS.escape(String(payload.id))}"]`);
    if (!row) return;

    // Update cached row state first so detail modals and follow-up renders stay in sync.
    const previousJobType = row.dataset.type || "";
    const status = payload.status || row.dataset.status || "queued";
    row.dataset.status = status;
    if (payload.message != null) {
        row.dataset.message = payload.message;
    }
    if (payload.url != null) {
        row.dataset.url = payload.url;
    }
    if (payload.type) {
        row.dataset.type = payload.type;
    }

    let sizeBytes = null;
    if (payload.filesize_bytes != null) {
        const parsedSize = Number(payload.filesize_bytes);
        sizeBytes = Number.isFinite(parsedSize) ? parsedSize : null;
        row.dataset.sizeBytes = sizeBytes != null ? String(sizeBytes) : "";
    } else if (row.dataset.sizeBytes) {
        const cachedSize = Number(row.dataset.sizeBytes);
        sizeBytes = Number.isFinite(cachedSize) ? cachedSize : null;
    }

    // Update BPM data attribute
    if (payload.bpm != null) {
        row.dataset.bpm = String(payload.bpm);
    }
    if (payload.bpm_confidence != null) {
        row.dataset.bpmConfidence = String(payload.bpm_confidence);
    }
    if (payload.progress != null) {
        const parsedProgress = Number(payload.progress);
        row.dataset.progress = Number.isFinite(parsedProgress) ? String(parsedProgress) : "";
    } else if (status !== "transcoding") {
        row.dataset.progress = "";
    }

    const titleCell = (payload.video_title != null || payload.video_meta_hover != null || payload.url != null)
        ? getCell(row, DATA_LABELS.TITLE)
        : null;
    const formatCell = payload.codec != null ? getCell(row, DATA_LABELS.FORMAT) : null;
    const bitrateCell = payload.bitrate_kbps != null ? getCell(row, DATA_LABELS.BITRATE) : null;
    const bpmCell = payload.bpm != null ? getCell(row, DATA_LABELS.BPM) : null;
    const statusCell = getCell(row, DATA_LABELS.STATUS);
    const actionCell = getCell(row, DATA_LABELS.ACTION);

    const jobType = row.dataset.type || payload.type || "";
    const nextActionCategory = getActionButtonCategory(status);
    const renderedActionCategory = getRenderedActionCategory(actionCell) || getRenderedActionCategory(statusCell);
    const shouldRefreshActions = renderedActionCategory == null
        || renderedActionCategory !== nextActionCategory
        || previousJobType !== jobType;

    // Apply DOM updates for the changed row.
    if (titleCell) {
        const titleText = titleCell.querySelector(".job-title-text");
        if (payload.video_title) {
            if (titleText) {
                titleText.textContent = payload.video_title;
            } else {
                titleCell.textContent = payload.video_title;
            }
        }

        const hoverText = payload.video_meta_hover
            || row.dataset.url
            || titleText?.textContent
            || titleCell.textContent
            || "";

        titleCell.dataset.popoverText = hoverText;
        titleCell.removeAttribute("title");
    }

    if (formatCell) {
        const codecText = formatCell.querySelector(".meta-sub");
        if (codecText) {
            codecText.textContent = payload.codec || "–";
        }
    }

    if (bitrateCell && payload.bitrate_kbps != null) {
        bitrateCell.textContent = `${payload.bitrate_kbps} kbps`;
    }

    if (bpmCell && payload.bpm != null) {
        bpmCell.textContent = Number(payload.bpm) > 0 ? String(payload.bpm) : "–";
    }

    // Update status display (inside .status-action-group if mobile layout)
    if (statusCell) {
        const statusInline = statusCell.querySelector(".status-inline");
        const statusGroup = statusCell.querySelector(".status-action-group");
        const newStatus = createStatusElement(status, sizeBytes, row.dataset.progress);

        if (statusInline) {
            // Preserve structure: just replace the status-inline element
            statusInline.replaceWith(newStatus);
        } else if (statusGroup) {
            // Mobile layout with action group: prepend new status, remove old if any
            const existing = statusGroup.querySelector(".status-inline");
            if (existing) existing.remove();
            statusGroup.prepend(newStatus);
        } else {
            // Fallback: replace entire cell content
            statusCell.replaceChildren(newStatus);
        }

        // Update mobile action button inside status cell
        if (shouldRefreshActions) {
            const mobileContainer = statusCell.querySelector(".d-mobile-only");
            if (mobileContainer) {
                mobileContainer.replaceChildren(createActionButton(payload.id, status, jobType));
            }
        }
    }

    // Class updates
    row.classList.toggle("row-done", TERMINAL_STATUSES.has(status));
    row.classList.toggle("row-error", status === "error");

    // Update desktop action button
    if (actionCell && shouldRefreshActions) {
        const actionWrap = actionCell.querySelector(".action-cell-wrap");
        if (actionWrap) {
            actionWrap.replaceChildren(createActionButton(payload.id, status, jobType));
        } else {
            actionCell.replaceChildren(createActionButton(payload.id, status, jobType));
        }
    }

    document.dispatchEvent(new CustomEvent("tubeyou:job-update", { detail: payload }));
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
            document.dispatchEvent(new CustomEvent("tubeyou:lalal-progress", { detail: payload }));
            return;
        }

        // Regular job update
        applyUpdate(payload);
    } catch (err) {
        console.warn("Malformed SSE event data:", err);
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
        setIndicator(indicator, true);
    };

    stream.onmessage = (event) => {
        handleMessage(event.data);
    };

    stream.onerror = () => {
        setIndicator(indicator, false);
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
        setIndicator(document.getElementById("wsIndicator"), false);
        return;
    }

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

window.addEventListener("pagehide", handlePageHide);
window.addEventListener("pageshow", handlePageShow);