//
// app/static/js/jobs.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module jobs
 *
 * Manages the jobs table DOM: building rows, paginated loading,
 * prepending new jobs, and trimming excess rows.
 *
 * State:
 *  - state.loading  Prevents concurrent loadMore() calls.
 *  - state.done     Suppresses loadMore() when all pages are fetched.
 *
 * Exports: buildRow, applyRowStatusClasses, prependJob, loadMore, resetPagingState
 */

import { CONFIG } from "./config.js";
import { createStatusElement, createActionButton } from "./ui.js";
import { EMPTY_VALUE } from "./utils.js";

const STATUS = Object.freeze({
    DONE: "done",
    ANALYSIS: "analysis",
    ANALYSIS_DONE: "analysis_done",
    ERROR: "error",
    QUEUED: "queued",
});

const JOB_TYPE = Object.freeze({
    AUDIO: "audio",
});

/** @type {{ loading: boolean, done: boolean }} */
const state = { loading: false, done: false };

/** @returns {HTMLTableSectionElement | null} */
function getTbody() {
    return document.getElementById("jobsTbody");
}

/**
 * Dispatches a global error event for upstream handlers.
 * @param {unknown} error
 */
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
    if (job?.type === JOB_TYPE.AUDIO) {
        return "MP3";
    }

    return job?.quality || "";
}

/**
 * Updates the status CSS classes on an existing job row.
 * Exported for use in WebSocket update handlers.
 * @param {HTMLTableRowElement} tr
 * @param {string} status
 */
export function applyRowStatusClasses(tr, status) {
    tr.classList.toggle("row-done",
        status === STATUS.DONE || status === STATUS.ANALYSIS_DONE
    );
    tr.classList.toggle("row-error", status === STATUS.ERROR);
    tr.dataset.status = status;
}

function trimRows() {
    const tbody = getTbody();
    if (!tbody) return;

    const rows = tbody.querySelectorAll("tr[data-job-id]");
    if (rows.length <= CONFIG.MAX_ROWS) return;

    // Efficiently trim excess rows by DOM traversal instead of getElementById loop.
    // Detail row immediately follows main row in DOM.
    for (let index = CONFIG.MAX_ROWS; index < rows.length; index += 1) {
        const mainRow = rows[index];
        // Check if detail row exists and remove it first
        const nextSibling = mainRow.nextElementSibling;
        if (nextSibling?.classList.contains("job-detail-row")) {
            nextSibling.remove();
        }
        mainRow.remove();
    }
}

/**
 * Builds a table row element for a single job.
 * @param {object} job
 * @returns {HTMLTableRowElement}
 */
export function buildRow(job) {
    const tr = document.createElement("tr");
    tr.dataset.jobId = String(job.id);
    tr.dataset.url = job.url || "";
    tr.dataset.status = job.status || STATUS.QUEUED;
    tr.dataset.type = job.type || "";
    tr.dataset.message = job.message || "";
    tr.dataset.sizeBytes = String(job.filesize_bytes ?? "");
    tr.dataset.bpm = String(job.bpm ?? "");
    tr.dataset.bpmConfidence = String(job.bpm_confidence ?? "");

    applyRowStatusClasses(tr, job.status || STATUS.QUEUED);

    const titleCell = document.createElement("td");
    titleCell.dataset.label = "Title";
    titleCell.className = "job-title-cell col-title";
    titleCell.dataset.popoverText = job.video_meta_hover || job.url || "";
    const titleText = document.createElement("span");
    titleText.className = "job-title-text";
    titleText.textContent = job.video_title || job.url || "";
    titleCell.appendChild(titleText);

    tr.appendChild(titleCell);

    // Format cell with codec sub-label (safe DOM construction, no innerHTML)
    const formatCell = document.createElement("td");
    formatCell.dataset.label = "Format";
    formatCell.className = "td-mono job-meta-cell col-compact";
    formatCell.appendChild(document.createTextNode(job.type || ""));
    const metaSub = document.createElement("div");
    metaSub.className = "meta-sub";
    metaSub.textContent = job.codec || EMPTY_VALUE;
    formatCell.appendChild(metaSub);
    tr.appendChild(formatCell);

    tr.appendChild(createCell("Quality", getQualityLabel(job), "td-mono job-meta-cell col-compact"));
    tr.appendChild(createCell("BPM", job.bpm ? String(job.bpm) : EMPTY_VALUE, "td-mono job-meta-cell col-compact"));
    tr.appendChild(createCell("Bitrate", job.bitrate_kbps ? `${job.bitrate_kbps} kbps` : EMPTY_VALUE, "td-mono job-meta-cell col-compact"));

    const statusCell = document.createElement("td");
    statusCell.dataset.label = "Status";
    statusCell.className = "col-status";
    statusCell.appendChild(createStatusElement(job.status, job.filesize_bytes));
    tr.appendChild(statusCell);

    const createdCell = document.createElement("td");
    createdCell.dataset.label = "Created";
    createdCell.className = "text-nowrap col-date";
    createdCell.textContent = job.created_at || "";
    tr.appendChild(createdCell);

    const actionCell = document.createElement("td");
    actionCell.dataset.label = "Action";
    actionCell.className = "col-actions";
    const actionWrap = document.createElement("div");
    actionWrap.className = "action-cell-wrap";
    actionWrap.appendChild(createActionButton(job.id, job.status, job.type));
    actionCell.appendChild(actionWrap);
    tr.appendChild(actionCell);

    return tr;
}

/**
 * Builds a hidden placeholder row paired with a job row.
 * Reserved for future expandable detail view functionality.
 * @param {object} job
 * @returns {HTMLTableRowElement}
 */
function buildDetailRow(job) {
    const tr = document.createElement("tr");
    tr.className = "job-detail-row d-none";
    tr.id = `detail-${String(job.id)}`;
    // Valid HTML: <tr> must have <td> children
    const td = document.createElement("td");
    td.colSpan = 7; // Match column count in buildRow
    tr.appendChild(td);
    return tr;
}

/**
 * Prepends a job row to the table, replacing any existing row with the same ID.
 * @param {object} job
 */
export function prependJob(job) {
    const tbody = getTbody();
    if (!tbody) return;

    const existing = tbody.querySelector(`tr[data-job-id="${CSS.escape(String(job.id))}"]`);
    if (existing) {
        // Remove the companion detail row together with the main row.
        document.getElementById(`detail-${String(job.id)}`)?.remove();
        existing.remove();
    }

    // Insert detail row first so prepend puts the main row on top of it.
    tbody.prepend(buildDetailRow(job));
    tbody.prepend(buildRow(job));
    // Allow users to paginate again after table mutations.
    state.done = false;
    trimRows();
}

/**
 * Resets pagination state so scroll-loading restarts from the beginning.
 * Call this after a full reload or filter change.
 */
export function resetPagingState() {
    state.done = false;
}

/**
 * Fetches and appends the next page of jobs to the table.
 * Skips duplicates by checking existing job IDs.
 * @param {(offset: number) => Promise<object[]>} fetchFn
 *   Function that accepts a row offset and resolves to a job array.
 * @returns {Promise<void>}
 */
export async function loadMore(fetchFn) {
    const tbody = getTbody();
    if (!tbody || state.loading || state.done) return;

    state.loading = true;

    // Snapshot row count from the DOM to avoid race conditions with prependJob
    // incrementing a shared counter while this fetch is in-flight.
    const currentOffset = tbody.querySelectorAll("tr[data-job-id]").length;

    try {
        const jobs = await fetchFn(currentOffset);

        if (!Array.isArray(jobs)) {
            dispatchLoadError(new TypeError("Expected jobs array from server"));
            state.done = true;
            return;
        }

        if (jobs.length === 0) {
            state.done = true;
            return;
        }

        // Build a Set of existing IDs for O(1) duplicate checks instead of
        // running a querySeletor per job inside the loop (O(n²)).
        const existingIds = new Set(
            Array.from(tbody.querySelectorAll("tr[data-job-id]"), (row) => row.dataset.jobId)
        );

        const fragment = document.createDocumentFragment();
        for (const job of jobs) {
            if (!existingIds.has(String(job.id))) {
                fragment.appendChild(buildRow(job));
                fragment.appendChild(buildDetailRow(job));
            }
        }

        if (fragment.childNodes.length > 0) {
            tbody.appendChild(fragment);
        }

        if (jobs.length < CONFIG.PAGE_SIZE) {
            state.done = true;
        }

        trimRows();
    } catch (error) {
        if (error?.name === "AbortError") {
            return;
        }
        console.error("Failed to load more jobs:", error);
        dispatchLoadError(error);
    } finally {
        state.loading = false;
    }
}

function init() {
    if (!getTbody()) return;
    trimRows();
    window.addEventListener("jobs-reload", resetPagingState);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
