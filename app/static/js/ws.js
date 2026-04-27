//
// app/static/js/ws.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// WebSocket client for real-time job updates.
// Requires the server-rendered jobs table to use English data-label values:
// Title, Format, Quality, Codec, Bitrate, Status, Created, Action.
//

import { CONFIG } from "./config.js";
import { createStatusElement, createActionButton } from "./ui.js";

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
    return row.querySelector(`td[data-label="${CSS.escape(label)}"]`);
}

/**
 * Cleanly tear down a WebSocket connection.
 * Immediately invalidates activeSocket if it matches the provided socket.
 * @param {WebSocket | null} ws - The WebSocket to tear down
 */
function teardown(ws) {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }

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
 * Start heartbeat monitoring.
 * Forces reconnect if no messages received within timeout.
 */
function startHeartbeat() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
    }

    lastMessageTime = Date.now();

    heartbeatTimer = setInterval(() => {
        const silence = Date.now() - lastMessageTime;
        if (silence > HEARTBEAT_TIMEOUT_MS) {
            console.warn(`WebSocket silent for ${Math.round(silence / 1000)}s, forcing reconnect`);
            if (activeSocket) {
                teardown(activeSocket);
            }
            // Trigger immediate reconnect
            reconnectAttempt = 0;
            connectWS();
        }
    }, HEARTBEAT_CHECK_MS);
}

/**
 * Apply a job status update to the DOM.
 * Batches DOM operations to minimize reflows.
 * @param {object} payload - The job update payload from the server
 */
function applyUpdate(payload) {
    if (!payload?.id) {
        console.warn("WebSocket update missing job id:", payload);
        return;
    }

    const row = document.querySelector(`tr[data-job-id="${CSS.escape(payload.id)}"]`);
    if (!row) return;

    // Batch all dataset updates first (no reflow)
    const status = payload.status || row.dataset.status || "queued";
    row.dataset.status = status;
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

    // Batch DOM reads
    const titleCell = getCell(row, "Title");
    const codecCell = getCell(row, "Codec");
    const bitrateCell = getCell(row, "Bitrate");
    const statusCell = getCell(row, "Status");
    const actionCell = getCell(row, "Action");

    // Batch DOM writes
    if (titleCell) {
        if (payload.video_title) {
            titleCell.textContent = payload.video_title;
        }
        titleCell.title = payload.video_meta_hover || row.dataset.url || titleCell.textContent || "";
    }

    if (codecCell && payload.codec) {
        codecCell.textContent = payload.codec;
    }

    if (bitrateCell && payload.bitrate_kbps != null) {
        bitrateCell.textContent = `${payload.bitrate_kbps} kbps`;
    }

    if (statusCell) {
        statusCell.replaceChildren(createStatusElement(status, sizeBytes));
    }

    // Class updates
    row.classList.remove("row-done", "row-error");
    if (status === "done") row.classList.add("row-done");
    if (status === "error") row.classList.add("row-error");

    if (actionCell) {
        actionCell.replaceChildren(createActionButton(payload.id, status, row.dataset.type || payload.type));
    }

    document.dispatchEvent(new CustomEvent("tubeyou:job-update", { detail: payload }));
}

/**
 * Handle incoming WebSocket message.
 * Responds to server pings and processes job updates.
 * @param {WebSocket} ws - The active WebSocket
 * @param {string} data - The raw message data
 */
function handleMessage(ws, data) {
    lastMessageTime = Date.now();

    // Server heartbeat ping - respond with pong
    if (data === "ping") {
        try {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send("pong");
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
    const indicator = document.getElementById("wsIndicator");
    const proto = location.protocol === "https:" ? "wss" : "ws";

    // Guard: Prevent parallel connections
    if (activeSocket?.readyState === WebSocket.OPEN || activeSocket?.readyState === WebSocket.CONNECTING) {
        return activeSocket;
    }

    // Clean up any stale socket
    if (activeSocket) {
        teardown(activeSocket);
    }

    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    activeSocket = ws;

    ws.onopen = () => {
        reconnectAttempt = 0;
        lastMessageTime = Date.now();
        setIndicator(indicator, true);
        startHeartbeat();
        ws.send("subscribe");
    };

    ws.onmessage = (event) => {
        handleMessage(ws, event.data);
    };

    ws.onerror = (err) => {
        console.error("WebSocket error:", err);
    };

    ws.onclose = () => {
        // Only clean up if this is still the active connection (race condition guard)
        if (activeSocket !== ws) {
            return;
        }

        setIndicator(indicator, false);
        activeSocket = null;

        if (heartbeatTimer) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }

        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
        }

        reconnectAttempt += 1;
        const baseDelay = Math.min(CONFIG.WS_RECONNECT_MS * (2 ** (reconnectAttempt - 1)), 30000);
        const jitter = Math.random() * 1000;
        reconnectTimer = setTimeout(connectWS, baseDelay + jitter);
    };

    return ws;
}

/**
 * Clean up WebSocket on page unload.
 */
function handleBeforeUnload() {
    if (activeSocket) {
        teardown(activeSocket);
    }
}

if (!window.__tubeyouWsCleanupRegistered) {
    window.addEventListener("beforeunload", handleBeforeUnload);
    window.__tubeyouWsCleanupRegistered = true;
}