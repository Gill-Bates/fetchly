//
// app/static/js/ws.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// WebSocket client for real-time job updates.
// Requires the server-rendered jobs table to use English data-label values:
// Title, Format, Quality, Bitrate, BPM, Status, Created, Action.
//

import { CONFIG } from "./config.js";
import { createStatusElement, createActionButton, getActionButtonCategory } from "./ui.js?v=20260429m";

const TERMINAL_STATUSES = new Set(["done", "analysis_done"]);

/** @type {number | null} */
let reconnectTimer = null;

/** @type {WebSocket | null} */
let activeSocket = null;

/** @type {number} */
let reconnectAttempt = 0;

/** @type {number | null} */
let heartbeatTimer = null;

/** @type {number} */
let lastMessageTime = 0;

/** Heartbeat interval in ms - should be slightly less than server ping interval */
const HEARTBEAT_CHECK_MS = 35000;

/** Max silence before forcing reconnect (server sends ping every 30s) */
const HEARTBEAT_TIMEOUT_MS = 50000;

function clearReconnectTimer() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

function clearHeartbeatTimer() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
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
    if (!root) return null;
    if (root.querySelector(".btn-group")) return "download";
    if (root.querySelector("[data-action='cancel-job']")) return "cancel";
    if (root.querySelector("[data-action='open-detail']")) return "detail";
    return null;
}

/**
 * Tear down a WebSocket connection cleanly.
 * Nulls event handlers to prevent stale callbacks, clears timers,
 * and invalidates activeSocket immediately when it matches the provided socket.
 * @param {WebSocket | null} ws - The WebSocket to tear down
 */
function teardown(ws) {
    clearReconnectTimer();
    clearHeartbeatTimer();

    if (!ws) {
        return;
    }

    // Immediate state cleanup - don't wait for async onclose
    if (activeSocket === ws) {
        activeSocket = null;
    }

    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;

    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        try {
            ws.close();
        } catch (err) {
            console.warn("WebSocket close error:", err);
        }
    }
}

/**
 * Schedule a reconnect attempt using exponential backoff and jitter.
 */
function scheduleReconnect() {
    clearReconnectTimer();
    setIndicator(document.getElementById("wsIndicator"), false);

    reconnectAttempt += 1;
    const baseDelay = Math.min(CONFIG.WS_RECONNECT_MS * (2 ** (reconnectAttempt - 1)), 30_000);
    const jitter = Math.random() * 1000;

    reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectWS();
    }, baseDelay + jitter);
}

/**
 * Start heartbeat monitoring.
 * Forces reconnect scheduling if no messages are received within timeout.
 */
function startHeartbeat() {
    clearHeartbeatTimer();

    lastMessageTime = Date.now();

    heartbeatTimer = setInterval(() => {
        const silence = Date.now() - lastMessageTime;
        if (silence > HEARTBEAT_TIMEOUT_MS) {
            console.warn(`WebSocket silent for ${Math.round(silence / 1000)}s, forcing reconnect`);

            const staleSocket = activeSocket;
            clearHeartbeatTimer();
            teardown(staleSocket);
            scheduleReconnect();
        }
    }, HEARTBEAT_CHECK_MS);
}

/**
 * Apply a job status update to the DOM.
 * @param {object} payload - The job update payload from the server
 */
function applyUpdate(payload) {
    if (!payload?.id) {
        console.warn("WebSocket update missing job id:", payload);
        return;
    }

    const row = document.querySelector(`tr[data-job-id="${escapeSelectorValue(payload.id)}"]`);
    if (!row) return;

    // Update cached row state first so detail modals and follow-up renders stay in sync.
    const previousStatus = row.dataset.status || "queued";
    const previousJobType = row.dataset.type || "";
    const status = payload.status || previousStatus;
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
        const newStatus = createStatusElement(status, sizeBytes);

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
 * Handle incoming WebSocket message.
 * Responds to server pings and processes job updates.
 * @param {string} data - The raw message data
 */
function handleMessage(data) {
    lastMessageTime = Date.now();

    // Server heartbeat ping - respond with pong
    if (typeof data === "string" && data === "ping") {
        try {
            if (activeSocket?.readyState === WebSocket.OPEN) {
                activeSocket.send("pong");
            }
        } catch (err) {
            console.warn("Failed to send pong:", err);
        }
        return;
    }

    // Job update payload
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
        console.warn("Malformed WebSocket frame:", err);
    }
}

/**
 * Establish a WebSocket connection for real-time job updates.
 * Implements reconnection with exponential backoff and heartbeat monitoring.
 * @returns {WebSocket} The active or newly created WebSocket instance
 */
export function connectWS() {
    clearReconnectTimer();

    const indicator = document.getElementById("wsIndicator");
    const proto = window.location.protocol === "https:" ? "wss" : "ws";

    // Guard: Prevent parallel connections
    if (activeSocket?.readyState === WebSocket.OPEN || activeSocket?.readyState === WebSocket.CONNECTING) {
        return activeSocket;
    }

    // Clean up any stale socket
    if (activeSocket) {
        teardown(activeSocket);
    }

    const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
    activeSocket = ws;

    ws.onopen = () => {
        reconnectAttempt = 0;
        lastMessageTime = Date.now();
        setIndicator(indicator, true);
        startHeartbeat();
        ws.send("subscribe");
    };

    ws.onmessage = (event) => {
        handleMessage(event.data);
    };

    ws.onerror = () => {
        console.error(`WebSocket error on ${ws.url} (readyState=${ws.readyState})`);
    };

    ws.onclose = () => {
        // Only clean up if this is still the active connection (race condition guard)
        if (activeSocket !== ws) {
            return;
        }

        activeSocket = null;
        clearHeartbeatTimer();
        scheduleReconnect();
    };

    return ws;
}

/**
 * Tear down the active WebSocket when the page is being hidden or unloaded.
 * Uses pagehide for better navigation and bfcache compatibility.
 */
function handlePageHide() {
    if (activeSocket) {
        teardown(activeSocket);
    }
}

window.addEventListener("pagehide", handlePageHide);