//
// app/static/js/ws.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";
import { createStatusElement, createActionButton } from "./ui.js";

let reconnectTimer = null;

function setIndicator(indicator, online) {
    if (!indicator) return;

    if (online) {
        indicator.textContent = "● LIVE";
        indicator.className = "ws-indicator badge bg-success live";
    } else {
        indicator.textContent = "● offline";
        indicator.className = "ws-indicator badge bg-danger";
    }
}

function applyUpdate(payload) {
    const row = document.querySelector(`[data-job-id="${payload.id}"]`);
    if (!row) return;

    const cells = row.querySelectorAll("td");
    if (cells.length < 8) return;

    row.dataset.status = payload.status || row.dataset.status || "queued";
    if (payload.filesize_bytes != null) {
        row.dataset.sizeBytes = String(payload.filesize_bytes);
    }

    const titleCell = cells[0];
    if (payload.video_title) {
        titleCell.textContent = payload.video_title;
    }
    titleCell.title = payload.video_meta_hover || row.dataset.url || titleCell.textContent || "";

    if (payload.codec) {
        cells[3].textContent = payload.codec;
    }
    if (payload.bitrate_kbps != null) {
        cells[4].textContent = `${payload.bitrate_kbps} kbps`;
    }

    const statusCell = cells[5];
    statusCell.replaceChildren(createStatusElement(row.dataset.status, row.dataset.sizeBytes || ""));

    row.classList.remove("row-done", "row-error");
    if (row.dataset.status === "done") row.classList.add("row-done");
    if (row.dataset.status === "error") row.classList.add("row-error");

    const actionCell = cells[7];
    actionCell.replaceChildren(createActionButton(payload.id, row.dataset.status));

    document.dispatchEvent(new CustomEvent("tubeyou:job-update", { detail: payload }));
}

export function connectWS() {
    const indicator = document.getElementById("wsIndicator");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);

    setIndicator(indicator, false);

    ws.onopen = () => {
        setIndicator(indicator, true);
        ws.send("subscribe");
    };

    ws.onmessage = (event) => {
        try {
            applyUpdate(JSON.parse(event.data));
        } catch {
            // Ignore malformed update frames.
        }
    };

    ws.onerror = () => {
        try {
            ws.close();
        } catch {
            // Ignore close errors.
        }
    };

    ws.onclose = () => {
        setIndicator(indicator, false);
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
        }
        reconnectTimer = setTimeout(connectWS, CONFIG.WS_RECONNECT_MS);
    };

    return ws;
}