//
// app/static/js/job.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { DOWNLOADABLE_STATUSES, TERMINAL_STATUSES } from "./config.js?v=20260831b";
import { getStatusPillClass, getStatusText, toProgressPercent } from "./ui.js?v=20260831c";
import { fetchJob } from "./api.js";

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
    // Re-derived from every payload, so a phase change with no progress/eta
    // clears the previous phase's bar and ETA.
    const pct = toProgressPercent(payload.progress);
    const hasProgress = pct !== null;
    progressRow.classList.toggle("d-none", !hasProgress);
    progressLabel.classList.toggle("d-none", !hasProgress);
    if (hasProgress) {
        progressBar.style.width = `${pct}%`;
        progressBar.setAttribute("aria-valuenow", String(pct));
    } else {
        progressBar.style.width = "0%";
        progressBar.setAttribute("aria-valuenow", "0");
    }

    if (payload.eta_seconds != null) {
        const seconds = Math.max(0, Math.round(Number(payload.eta_seconds) || 0));
        etaText.textContent = seconds > 60
            ? `~${Math.round(seconds / 60)} min remaining`
            : `~${seconds}s remaining`;
    } else {
        etaText.textContent = "";
    }
}

function applyPayload(payload) {
    if (!payload || String(payload.id) !== jobId) {
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

    // Downloadable != terminal ("analysis" is downloadable), so toggle the
    // link on every status update.
    if (payload.status) {
        linkEl.classList.toggle("d-none", !DOWNLOADABLE_STATUSES.has(payload.status));
    }

    if (payload.status && TERMINAL_STATUSES.has(payload.status)) {
        shouldReconnect = false;
        stream?.close();
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
        // Session gone server-side: stop reconnecting and close before
        // navigating so a late event cannot reopen the stream.
        shouldReconnect = false;
        stream?.close();
        window.location.replace("/login");
        return;
    }

    if (payload.type === "resync_required") {
        if (payload.job_id && String(payload.job_id) !== jobId) {
            return;
        }
        void reconcileJob().catch(() => {
            // The normal EventSource reconnect/error path will retry recovery.
        });
        return;
    }

    if (String(payload.id) !== jobId) {
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
