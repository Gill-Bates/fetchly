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
 *  - state.nextOffset  Offset used for the next LIMIT/OFFSET request.
 *  - state.preserveHistory  Stops row trimming once the user starts loading
 *                           older pages so fetched history is not discarded.
 *
 * Exports: buildRow, applyRowStatusClasses, prependJob, loadMore, resetPagingState
 */

import { CONFIG } from "./config.js";
import { createStatusElement, createActionButton } from "./ui.js?v=20260429m";
import { EMPTY_VALUE } from "./utils.js";

const FALLBACK_JOBS_TABLE_COLUMN_COUNT = 8;

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

let cachedTableColumnCount = null;

/** @type {{ loading: boolean, done: boolean, nextOffset: number, preserveHistory: boolean }} */
const state = { loading: false, done: false, nextOffset: 0, preserveHistory: false };

function createCopyUrlButton(url) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "job-copy-url-btn";
    button.dataset.copyUrl = url || "";
    button.setAttribute("aria-label", "Copy source URL");
    button.title = "Copy source URL";

    const icon = document.createElement("span");
    icon.className = "material-symbols-outlined";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "content_copy";
    button.append(icon);

    return button;
}

/** @returns {HTMLTableSectionElement | null} */
function getTbody() {
    return document.getElementById("jobsTbody");
}

function getRenderedJobCount() {
    const tbody = getTbody();
    return tbody ? tbody.querySelectorAll("tr[data-job-id]").length : 0;
}

function getJobsTableColumnCount() {
    if (cachedTableColumnCount !== null) {
        return cachedTableColumnCount;
    }

    const tbody = getTbody();
    const headerCells = tbody?.closest("table")?.tHead?.rows?.[0]?.cells;
    cachedTableColumnCount = headerCells?.length || FALLBACK_JOBS_TABLE_COLUMN_COUNT;
    return cachedTableColumnCount;
}

/**
 * Build the Created cell using safe DOM APIs only.
 * Invalid values are rendered as plain text.
 * @param {unknown} isoOrFormatted
 * @returns {HTMLTableCellElement}
 */
function buildCreatedCell(isoOrFormatted) {
    const td = document.createElement("td");
    td.dataset.label = "Created";
    td.className = "text-nowrap col-date";

    const text = isoOrFormatted == null ? "" : String(isoOrFormatted);
    const dt = new Date(text);
    if (Number.isNaN(dt.getTime())) {
        td.textContent = text;
        return td;
    }

    const datePart = document.createElement("span");
    datePart.className = "date-part";
    datePart.textContent = dt.toLocaleDateString(undefined, {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
    });

    const timePart = document.createElement("span");
    timePart.className = "time-part";
    timePart.textContent = dt.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
    });

    td.append(datePart, " ", timePart);
    return td;
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
    tr.classList.toggle(
        "row-done",
        status === STATUS.DONE || status === STATUS.ANALYSIS_DONE,
    );
    tr.classList.toggle("row-error", status === STATUS.ERROR);
    tr.dataset.status = status;
}

function trimRows() {
    const tbody = getTbody();
    if (!tbody || state.preserveHistory) return;

    const rows = tbody.querySelectorAll("tr[data-job-id]");
    if (rows.length <= CONFIG.MAX_ROWS) return;

    for (let index = CONFIG.MAX_ROWS; index < rows.length; index += 1) {
        const mainRow = rows[index];
        const jobId = mainRow.dataset.jobId;
        if (jobId) {
            document.getElementById(`detail-${jobId}`)?.remove();
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
    const jobId = String(job.id);
    const tr = document.createElement("tr");
    tr.dataset.jobId = jobId;
    tr.dataset.url = job.url || "";
    tr.dataset.status = job.status || STATUS.QUEUED;
    tr.dataset.type = job.type || "";
    tr.dataset.message = job.message || "";
    tr.dataset.sizeBytes = String(job.filesize_bytes ?? "");
    tr.dataset.bpm = String(job.bpm ?? "");
    tr.dataset.bpmConfidence = String(job.bpm_confidence ?? "");
    tr.dataset.progress = String(job.progress ?? "");

    applyRowStatusClasses(tr, job.status || STATUS.QUEUED);

    const titleCell = document.createElement("td");
    titleCell.dataset.label = "Title";
    titleCell.className = "job-title-cell job-title-cell--wide col-title";
    titleCell.dataset.popoverText = job.video_meta_hover || job.url || "";
    titleCell.append(createCopyUrlButton(job.url || ""));

    const titleText = document.createElement("span");
    titleText.className = "job-title-text";
    titleText.textContent = job.video_title || job.url || "";
    titleCell.append(titleText);
    tr.append(titleCell);

    const formatCell = document.createElement("td");
    formatCell.dataset.label = "Format";
    formatCell.className = "td-mono job-meta-cell col-compact";
    formatCell.textContent = job.type || "";

    const metaSub = document.createElement("div");
    metaSub.className = "meta-sub";
    metaSub.textContent = job.codec || EMPTY_VALUE;
    formatCell.append(metaSub);
    tr.append(formatCell);

    tr.append(createCell("Quality", getQualityLabel(job), "td-mono job-meta-cell col-compact"));
    tr.append(createCell("BPM", job.bpm ? String(job.bpm) : EMPTY_VALUE, "td-mono job-meta-cell col-compact"));
    tr.append(createCell("Bitrate", job.bitrate_kbps ? `${job.bitrate_kbps} kbps` : EMPTY_VALUE, "td-mono job-meta-cell col-compact"));

    const statusCell = document.createElement("td");
    statusCell.dataset.label = "Status";
    statusCell.className = "col-status";

    const statusActionGroup = document.createElement("div");
    statusActionGroup.className = "status-action-group";
    statusActionGroup.append(createStatusElement(job.status, job.filesize_bytes, job.progress));

    const actionButton = createActionButton(jobId, job.status, job.type);

    const mobileActionWrap = document.createElement("div");
    mobileActionWrap.className = "d-mobile-only";
    mobileActionWrap.append(actionButton.cloneNode(true));
    statusActionGroup.append(mobileActionWrap);

    statusCell.append(statusActionGroup);
    tr.append(statusCell);
    tr.append(buildCreatedCell(job.created_at));

    const actionCell = document.createElement("td");
    actionCell.dataset.label = "Action";
    actionCell.className = "col-actions";

    const actionWrap = document.createElement("div");
    actionWrap.className = "action-cell-wrap";
    actionWrap.append(actionButton);
    actionCell.append(actionWrap);
    tr.append(actionCell);

    return tr;
}

/**
 * Build the hidden companion row paired with a job row.
 * The row is addressed by `detail-{jobId}` from live-update handlers.
 * @param {object} job
 * @returns {HTMLTableRowElement}
 */
function buildDetailRow(job) {
    const jobId = String(job.id);
    const tr = document.createElement("tr");
    tr.className = "job-detail-row d-none";
    tr.id = `detail-${jobId}`;

    const td = document.createElement("td");
    td.colSpan = getJobsTableColumnCount();
    tr.append(td);
    return tr;
}

/**
 * Prepends a job row to the table, replacing any existing row with the same ID.
 * @param {object} job
 */
export function prependJob(job) {
    const tbody = getTbody();
    if (!tbody) return;

    const jobId = String(job.id);
    const renderedCountBeforePrepend = getRenderedJobCount();

    const existing = tbody.querySelector(`tr[data-job-id="${CSS.escape(jobId)}"]`);
    if (existing) {
        document.getElementById(`detail-${jobId}`)?.remove();
        existing.remove();
    }

    tbody.prepend(buildDetailRow(job));
    tbody.prepend(buildRow(job));
    state.done = false;
    trimRows();

    if (!existing && (state.preserveHistory || renderedCountBeforePrepend < CONFIG.MAX_ROWS)) {
        state.nextOffset += 1;
    }
}

/**
 * Resets pagination state so scroll-loading restarts from the beginning.
 * Call this after a full reload or filter change.
 */
export function resetPagingState() {
    cachedTableColumnCount = null;
    state.done = false;
    state.preserveHistory = false;
    state.nextOffset = 0;
}

/**
 * Fetch and append the next page of jobs to the table.
 * Skips jobs whose IDs already exist in the table.
 * Appends both a main row and a hidden detail row per job.
 * Dispatches `jobs-load-error` on non-abort failures.
 * Sets `state.done = true` when the server returns a non-array payload,
 * an empty page, or a final partial page.
 * @param {(offset: number) => Promise<object[]>} fetchFn
 *   Function that accepts a row offset and resolves to a job array.
 * @returns {Promise<void>}
 */
export async function loadMore(fetchFn) {
    const tbody = getTbody();
    if (!tbody || state.loading || state.done) return;

    state.loading = true;

    // Snapshot the current offset before awaiting so concurrent prependJob()
    // updates cannot change which server page this request asks for.
    const currentOffset = state.nextOffset;

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

        // With LIMIT/OFFSET pagination, trimming older rows after history loads
        // would make later offsets skip or discard fetched pages.
        state.preserveHistory = true;

        const expectedNextOffset = currentOffset + jobs.length;
        state.nextOffset = expectedNextOffset;

        const existingIds = new Set(
            Array.from(tbody.querySelectorAll("tr[data-job-id]"), (row) => row.dataset.jobId),
        );

        const fragment = document.createDocumentFragment();
        for (const job of jobs) {
            const jobId = String(job.id);
            if (existingIds.has(jobId)) {
                continue;
            }

            existingIds.add(jobId);
            fragment.append(buildRow(job), buildDetailRow(job));
        }

        if (fragment.childNodes.length > 0) {
            tbody.append(fragment);
        }

        if (jobs.length < CONFIG.PAGE_SIZE) {
            state.done = true;
        }

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
    state.nextOffset = getRenderedJobCount();
    trimRows();
    window.removeEventListener("jobs-reload", resetPagingState);
    window.addEventListener("jobs-reload", resetPagingState);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
