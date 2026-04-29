//
// app/static/js/events.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Server-sent event client for real-time job updates.
// Requires the server-rendered jobs table to use English data-label values:
// Title, Format, Quality, Bitrate, BPM, Status, Created, Action.
//

import { CONFIG } from "./config.js";
import { TERMINAL_STATUSES } from "./config.js";
import { createStatusElement, createActionButton, getActionButtonCategory } from "./ui.js?v=20260429m";

/** @type {number | null} */
let reconnectTimer = null;

/** @type {EventSource | null} */
let activeStream = null;

/** @type {number} */
let reconnectAttempt = 0;
let shouldReconnect = true;

function clearReconnectTimer() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

function escapeSelectorValue(value) {
    return CSS.escape(String(value));
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
    return row.querySelector(`td[data-label="${escapeSelectorValue(label)}"]`);
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
    if (!shouldReconnect) {
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
 * @param {object} payload - The job update payload from the server
 */
function applyUpdate(payload) {
    if (!payload?.id) {
        console.warn("SSE update missing job id:", payload);
        return;
    }

    const row = document.querySelector(`tr[data-job-id="${escapeSelectorValue(payload.id)}"]`);
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
        ? getCell(row, "Title")
        : null;
    const formatCell = payload.codec != null ? getCell(row, "Format") : null;
    const bitrateCell = payload.bitrate_kbps != null ? getCell(row, "Bitrate") : null;
    const bpmCell = payload.bpm != null ? getCell(row, "BPM") : null;
    const statusCell = getCell(row, "Status");
    const actionCell = getCell(row, "Action");

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

    if (formatCell && payload.codec != null) {
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
 * @returns {EventSource} The active or newly created event stream
 */
export function connectEventStream() {
    clearReconnectTimer();
    shouldReconnect = true;

    const indicator = document.getElementById("wsIndicator");

    if (activeStream && activeStream.readyState !== EventSource.CLOSED) {
        return activeStream;
    }

    if (activeStream) {
        teardown(activeStream);
    }

    const stream = new EventSource("/events");
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
        connectEventStream();
    }
}

window.addEventListener("pagehide", handlePageHide);
window.addEventListener("pageshow", handlePageShow);