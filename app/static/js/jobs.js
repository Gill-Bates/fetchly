//
// app/static/js/jobs.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";
import { createStatusElement, createActionButton } from "./ui.js";

const tbody = document.getElementById("jobsTbody");
const hasTable = Boolean(tbody);

let offset = hasTable ? document.querySelectorAll("#jobsTbody tr[data-job-id]").length : 0;
let loading = false;
let done = false;

function createCell(label, text) {
    const td = document.createElement("td");
    td.dataset.label = label;
    td.textContent = text;
    return td;
}

function trimRows() {
    if (!hasTable) return;

    const rows = tbody.querySelectorAll("tr[data-job-id]");
    if (rows.length <= CONFIG.MAX_ROWS) return;

    for (let index = CONFIG.MAX_ROWS; index < rows.length; index += 1) {
        rows[index].remove();
    }
}

export function buildRow(job) {
    const tr = document.createElement("tr");
    tr.dataset.jobId = job.id;
    tr.dataset.url = job.url || "";
    tr.dataset.status = job.status || "queued";
    tr.dataset.sizeBytes = job.filesize_bytes ?? "";

    if (job.status === "done") tr.classList.add("row-done");
    if (job.status === "error") tr.classList.add("row-error");

    const titleCell = document.createElement("td");
    titleCell.dataset.label = "Titel";
    titleCell.className = "text-truncate";
    titleCell.style.maxWidth = "220px";
    titleCell.title = job.video_meta_hover || job.url || "";
    titleCell.textContent = job.video_title || job.url || "";

    tr.appendChild(titleCell);
    tr.appendChild(createCell("Format", job.type || ""));
    tr.appendChild(createCell("Quality", job.quality || ""));
    tr.appendChild(createCell("Codec", job.codec || "-"));
    tr.appendChild(createCell("Bitrate", job.bitrate_kbps ? `${job.bitrate_kbps} kbps` : "-"));

    const statusCell = document.createElement("td");
    statusCell.dataset.label = "Status";
    statusCell.appendChild(createStatusElement(job.status, job.filesize_bytes));
    tr.appendChild(statusCell);

    const createdCell = document.createElement("td");
    createdCell.dataset.label = "Created";
    createdCell.className = "text-nowrap";
    createdCell.textContent = job.created_at || "";
    tr.appendChild(createdCell);

    const actionCell = document.createElement("td");
    actionCell.dataset.label = "Action";
    actionCell.appendChild(createActionButton(job.id, job.status));
    tr.appendChild(actionCell);

    return tr;
}

export function prependJob(job) {
    if (!hasTable) return;

    const existing = tbody.querySelector(`tr[data-job-id="${job.id}"]`);
    if (existing) {
        existing.remove();
    }

    tbody.prepend(buildRow(job));
    if (!existing) {
        offset += 1;
    }
    trimRows();
}

export async function loadMore(fetchFn) {
    if (!hasTable || loading || done) return;
    loading = true;

    try {
        const jobs = await fetchFn(offset);
        if (!Array.isArray(jobs) || jobs.length === 0) {
            done = true;
            return;
        }

        for (const job of jobs) {
            if (!tbody.querySelector(`tr[data-job-id="${job.id}"]`)) {
                tbody.appendChild(buildRow(job));
            }
        }

        offset += jobs.length;
        if (jobs.length < CONFIG.PAGE_SIZE) {
            done = true;
        }

        trimRows();
    } catch {
        // Best-effort paging; keep the table usable on transient network errors.
    } finally {
        loading = false;
    }
}