//
// app/static/js/job.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { DOWNLOADABLE_STATUSES, TERMINAL_STATUSES } from "/static/js/config.js?v=20260823b";
import { getStatusPillClass, getStatusText } from "/static/js/ui.js?v=20260823c";
import { fetchJob } from "/static/js/api.js";

const script = document.getElementById("jobStatusScript");
const jobId = script?.dataset.jobId;
const initialStatus = script?.dataset.initialStatus || "";

if (!jobId) {
    throw new Error("Missing job status script configuration");
}

const statusEl = document.getElementById("status");
const messageEl = document.getElementById("message");
const bpmEl = document.getElementById("bpmValue");
const bpmRowEl = document.getElementById("bpmRow");
const confidenceEl = document.getElementById("bpmConfidence");
const linkEl = document.getElementById("downloadLink");
const progressBar = document.getElementById("progressBar");
const progressRow = document.getElementById("progressRow");
const progressLabel = document.getElementById("progressLabel");
const etaText = document.getElementById("etaText");
const statusText = document.getElementById("statusText");

let shouldReconnect = !TERMINAL_STATUSES.has(initialStatus);
let stream = null;

function updateStatus(payload) {
    statusText.textContent = getStatusText(payload.status, payload.progress);
    statusEl.className = getStatusPillClass(payload.status);
}

function updateProgress(payload) {
    if (payload.progress != null) {
        progressRow.classList.remove("d-none");
        progressLabel.classList.remove("d-none");
        const pct = Math.max(0, Math.min(100, Number(payload.progress) || 0));
        progressBar.style.width = `${pct}%`;
        progressBar.setAttribute("aria-valuenow", String(pct));
    }

    if (payload.eta_seconds != null) {
        const seconds = Math.max(0, Math.round(Number(payload.eta_seconds) || 0));
        etaText.textContent = seconds > 60
            ? `~${Math.round(seconds / 60)} min remaining`
            : `~${seconds}s remaining`;
    }
}

function applyPayload(payload) {
    if (!payload || String(payload.id) !== String(jobId)) {
        return;
    }

    updateStatus(payload);
    messageEl.textContent = payload.message || "–";

    if (payload.bpm != null) {
        bpmEl.textContent = String(payload.bpm);
        bpmRowEl.classList.remove("text-muted");
    }

    if (payload.bpm_confidence != null) {
        confidenceEl.textContent = Number(payload.bpm_confidence).toFixed(2);
    }

    updateProgress(payload);

    if (payload.status && TERMINAL_STATUSES.has(payload.status)) {
        shouldReconnect = false;
        stream?.close();
        etaText.textContent = "";
        progressRow.classList.add("d-none");
        progressLabel.classList.add("d-none");
        linkEl.classList.toggle("d-none", !DOWNLOADABLE_STATUSES.has(payload.status));
    }
}

async function reconcileJob() {
    const payload = await fetchJob(jobId);
    applyPayload(payload);
}

function handleMessage(event) {
    let payload;
    try {
        payload = JSON.parse(event.data);
    } catch {
        return;
    }
    if (payload.type === "authentication_required") {
        shouldReconnect = false;
        stream?.close();
        return;
    }

    if (payload.type === "resync_required") {
        if (payload.job_id && payload.job_id !== jobId) {
            return;
        }
        void reconcileJob().catch(() => {
            // The normal EventSource reconnect/error path will retry recovery.
        });
        return;
    }

    if (payload.id !== jobId) {
        return;
    }

    applyPayload(payload);
}

function connect() {
    if (!shouldReconnect || (stream && stream.readyState !== EventSource.CLOSED)) {
        return;
    }

    stream = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`, { withCredentials: true });
    stream.onopen = () => {
        void reconcileJob().catch(() => {
            // The SSE stream remains the primary update path.
        });
    };
    stream.onmessage = handleMessage;
    stream.onerror = () => {
        if (!shouldReconnect) {
            return;
        }

        const reconnectSuffix = " · RECONNECTING";
        if (!statusText.textContent.endsWith(reconnectSuffix)) {
            statusText.textContent = `${statusText.textContent}${reconnectSuffix}`;
        }
    };
}

if (shouldReconnect) {
    connect();
}

window.addEventListener("pagehide", () => stream?.close());
window.addEventListener("pageshow", (event) => {
    if (event.persisted && shouldReconnect) {
        connect();
    }
});
