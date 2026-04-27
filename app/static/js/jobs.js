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

function dispatchLoadError(error) {
    const detail = error instanceof Error ? error : new Error(String(error));
    window.dispatchEvent(new CustomEvent("jobs-load-error", { detail }));
}

function createCell(label, text, className = "") {
    const td = document.createElement("td");
    td.dataset.label = label;
    if (className) {
        td.className = className;
    }
    td.textContent = text;
    return td;
}

function getQualityLabel(job) {
    if (job?.type === "audio") {
        return "MP3";
    }

    return job?.quality || "";
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
    tr.dataset.type = job.type || "";
    tr.dataset.sizeBytes = job.filesize_bytes ?? "";

    if (job.status === "done") tr.classList.add("row-done");
    if (job.status === "error") tr.classList.add("row-error");

    const titleCell = document.createElement("td");
    titleCell.dataset.label = "Title";
    titleCell.className = "text-truncate job-title-cell job-title-cell--compact";
    titleCell.title = job.video_meta_hover || job.url || "";
    const titleText = document.createElement("span");
    titleText.className = "job-title-text";
    titleText.textContent = job.video_title || job.url || "";
    titleCell.appendChild(titleText);

    tr.appendChild(titleCell);
    tr.appendChild(createCell("Format", job.type || "", "td-mono job-meta-cell"));
    tr.appendChild(createCell("Quality", getQualityLabel(job), "td-mono job-meta-cell"));
    tr.appendChild(createCell("Codec", job.codec || "-", "td-mono job-meta-cell"));
    tr.appendChild(createCell("Bitrate", job.bitrate_kbps ? `${job.bitrate_kbps} kbps` : "-", "td-mono job-meta-cell"));

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
    actionCell.appendChild(createActionButton(job.id, job.status, job.type));
    tr.appendChild(actionCell);

    return tr;
}

export function prependJob(job) {
    if (!hasTable) return;

    const existing = tbody.querySelector(`tr[data-job-id="${CSS.escape(job.id)}"]`);
    if (existing) {
        existing.remove();
    }

    tbody.prepend(buildRow(job));
    if (!existing) {
        offset += 1;
    }
    // Allow users to paginate again after table mutations.
    done = false;
    trimRows();
}

export function resetPagingState() {
    if (!hasTable) return;
    offset = tbody.querySelectorAll("tr[data-job-id]").length;
    done = false;
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

        const fragment = document.createDocumentFragment();
        for (const job of jobs) {
            if (!tbody.querySelector(`tr[data-job-id="${CSS.escape(job.id)}"]`)) {
                fragment.appendChild(buildRow(job));
            }
        }

        if (fragment.childNodes.length) {
            tbody.appendChild(fragment);
        }

        offset += jobs.length;
        if (jobs.length < CONFIG.PAGE_SIZE) {
            done = true;
        }

        trimRows();
    } catch (error) {
        if (error?.name === "AbortError") {
            return;
        }
        console.error("Failed to load more jobs:", error);
        dispatchLoadError(error);
    } finally {
        loading = false;
    }
}

if (hasTable) {
    trimRows();
    window.addEventListener("jobs-reload", resetPagingState);
}
