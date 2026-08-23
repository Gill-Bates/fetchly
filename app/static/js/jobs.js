//
// app/static/js/jobs.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module jobs
 *
 * Manages the jobs collection DOM for both desktop and mobile renderers.
 * Desktop remains table-based, while mobile renders a feed of articles from
 * the same in-memory job list while maintaining parallel desktop and mobile
 * DOM representations.
 */

import { CONFIG, TERMINAL_STATUSES } from "./config.js";
import { reportError } from "./errors.js";
import {
    createStatusElement,
    createActionButton,
    createPrimaryActionButton,
    resolveJobAction,
} from "./ui.js?v=20260823c";
import { detectPlatform, humanSize, platformPillLabel } from "./utils.js";

const MOBILE_BREAKPOINT = "(max-width: 1024px)";
const FALLBACK_JOBS_TABLE_COLUMN_COUNT = 8;
const HARD_MAX_ROWS = CONFIG.MAX_ROWS * 2;
const EMPTY_VALUE = "–";

function parseUtcTimestamp(value) {
    const text = value == null ? "" : String(value).trim();
    const sqliteUtc = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
    return new Date(sqliteUtc.test(text) ? `${text.replace(" ", "T")}Z` : text);
}

const STATUS = Object.freeze({
    DOWNLOADING: "downloading",
    DONE: "done",
    ANALYSIS: "analysis",
    ANALYSIS_DONE: "analysis_done",
    CANCELLED: "cancelled",
    ERROR: "error",
    PROCESSING: "processing",
    QUEUED: "queued",
    TRANSCODING: "transcoding",
});

const JOB_TYPE = Object.freeze({
    AUDIO: "audio",
});

const DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
});

const TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
});

const MOBILE_DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
});

const mediaQuery = typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(MOBILE_BREAKPOINT)
    : null;

/** @type {{ loading: boolean, done: boolean, nextOffset: number, preserveHistory: boolean, jobs: object[], jobIds: Set<string>, desktopNodes: Map<string, HTMLElement>, mobileNodes: Map<string, HTMLElement>, mobileView: boolean, initialized: boolean }} */
const state = {
    loading: false,
    done: false,
    nextOffset: 0,
    preserveHistory: false,
    jobs: [],
    jobIds: new Set(),
    desktopNodes: new Map(),
    mobileNodes: new Map(),
    mobileView: mediaQuery?.matches ?? false,
    initialized: false,
};

/** @returns {HTMLTableSectionElement | null} */
function getTbody() {
    return document.getElementById("jobsTbody");
}

function getMobileList() {
    return document.getElementById("jobsMobileList");
}

function getBootstrapNode() {
    return document.getElementById("jobsBootstrapData");
}

function getDesktopView() {
    return getTbody()?.closest(".jobs-desktop-view") ?? null;
}

function getJobId(job) {
    return String(job?.id ?? "");
}

function getJobsTableColumnCount() {
    const tbody = getTbody();
    const headerCells = tbody?.closest("table")?.tHead?.rows?.[0]?.cells;
    return headerCells?.length || FALLBACK_JOBS_TABLE_COLUMN_COUNT;
}

function getTitleText(job) {
    return job?.video_title || job?.url || "";
}

function getQualityLabel(job) {
    if (job?.type === JOB_TYPE.AUDIO) {
        return String(job?.quality || job?.codec || "MP3").toUpperCase();
    }

    const quality = String(job?.quality || EMPTY_VALUE);
    return quality === "max" ? "Max" : quality;
}

function getTypeLabel(job) {
    const type = String(job?.type || "").toLowerCase();
    if (type === "audio") return "Audio";
    if (type === "video") return "Video";
    return type ? type.charAt(0).toUpperCase() + type.slice(1) : EMPTY_VALUE;
}

function formatBpmValue(value) {
    const bpm = Number(value);
    return Number.isFinite(bpm) && bpm > 0 ? String(Math.round(bpm)) : EMPTY_VALUE;
}

function formatBpmCompact(value) {
    const bpm = formatBpmValue(value);
    return bpm === EMPTY_VALUE ? EMPTY_VALUE : `${bpm} BPM`;
}

function formatBitrateText(value) {
    const bitrate = Number(value);
    return Number.isFinite(bitrate) && bitrate > 0 ? `${Math.round(bitrate)} kbps` : EMPTY_VALUE;
}

export function formatCompactJobMeta(jobLike) {
    const parts = [
        jobLike?.type || "",
        getQualityLabel(jobLike),
        formatBpmCompact(jobLike?.bpm),
        formatBitrateText(jobLike?.bitrate_kbps),
    ].filter((part) => part && part !== EMPTY_VALUE);
    return parts.join(" · ") || EMPTY_VALUE;
}

function formatMobileMediaLine(jobLike) {
    const parts = [
        getTypeLabel(jobLike),
        getQualityLabel(jobLike),
        humanSize(jobLike?.filesize_bytes),
    ].filter((part) => part && part !== EMPTY_VALUE);
    return parts.join(" · ") || EMPTY_VALUE;
}

function formatMobileTimeLine(isoOrFormatted) {
    const text = isoOrFormatted == null ? "" : String(isoOrFormatted);
    const dt = parseUtcTimestamp(text);
    return Number.isNaN(dt.getTime()) ? text : MOBILE_DATE_TIME_FORMATTER.format(dt);
}

export function formatCreatedText(isoOrFormatted) {
    const text = isoOrFormatted == null ? "" : String(isoOrFormatted);
    const dt = parseUtcTimestamp(text);
    if (Number.isNaN(dt.getTime())) {
        return text;
    }

    return `${dt.toLocaleDateString(undefined, {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
    })} ${dt.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
    })}`;
}

function normalizeNumberField(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function hasOwnField(payload, key) {
    return Object.prototype.hasOwnProperty.call(payload, key);
}

function findStoredJobIndex(jobId) {
    return state.jobs.findIndex((job) => getJobId(job) === jobId);
}

function getStoredJob(jobId) {
    const index = findStoredJobIndex(jobId);
    return index === -1 ? null : state.jobs[index];
}

function replaceJobs(nextJobs) {
    state.jobs = nextJobs;
    state.jobIds = new Set(nextJobs.map((job) => getJobId(job)).filter(Boolean));
}

/**
 * Return a stored job by id.
 * @param {string} jobId
 * @returns {object | null}
 */
export function getJobById(jobId) {
    return getStoredJob(String(jobId || ""));
}

function getRenderedNodesMap({ mobile = false } = {}) {
    return mobile ? state.mobileNodes : state.desktopNodes;
}

function clearContainerEmptyState(container) {
    container?.querySelector("#emptyRow, #jobsEmptyState")?.remove();
}

function trimRenderedNodes(container, renderedNodes) {
    if (!(container instanceof Element)) {
        return;
    }

    while (renderedNodes.size > state.jobs.length) {
        const trailingNode = container.lastElementChild;
        if (!(trailingNode instanceof HTMLElement)) {
            return;
        }

        renderedNodes.delete(trailingNode.dataset.jobId || "");
        trailingNode.remove();
    }
}

function preserveRenderState(source, target) {
    if (source.classList.contains("d-none")) {
        target.classList.add("d-none");
    }
}

function syncSurfaceVisibility() {
    getDesktopView()?.classList.toggle("d-none", state.mobileView);
    getMobileList()?.classList.toggle("d-none", !state.mobileView);
}

/**
 * Normalize a partial job update from SSE or action responses.
 * Only defined fields are copied so store merges can preserve existing data.
 * @param {object | null | undefined} payload
 * @returns {object | null}
 */
export function normalizeJobUpdate(payload) {
    if (!payload || payload.id == null) {
        return null;
    }

    const update = { id: String(payload.id) };

    for (const key of ["status", "message", "url", "type", "video_title", "video_meta_hover", "codec", "quality", "filename"]) {
        if (!hasOwnField(payload, key)) {
            continue;
        }

        const value = payload[key];
        if (value == null) {
            update[key] = null;
        } else if (typeof value === "string") {
            update[key] = value;
        }
    }

    for (const key of ["bitrate_kbps", "bpm", "bpm_confidence", "filesize_bytes", "progress", "duration_seconds"]) {
        if (!hasOwnField(payload, key)) {
            continue;
        }

        update[key] = payload[key] == null ? null : normalizeNumberField(payload[key]);
    }

    if (hasOwnField(payload, "created_at")) {
        update.created_at = payload.created_at == null ? null : String(payload.created_at);
    }

    if (hasOwnField(payload, "finished_at")) {
        update.finished_at = payload.finished_at == null ? null : String(payload.finished_at);
    }

    return update;
}

function mergeStoredJob(existingJob, update) {
    const mergedJob = { ...existingJob, ...update };

    if (hasOwnField(update, "status") && update.status !== STATUS.TRANSCODING && !hasOwnField(update, "progress")) {
        mergedJob.progress = null;
    }

    return mergedJob;
}

/**
 * Merge a normalized partial update into the in-memory job store.
 * @param {object | null} update
 * @returns {object | null}
 */
export function updateJobStore(update) {
    if (!update?.id) {
        return null;
    }

    const index = findStoredJobIndex(update.id);
    if (index === -1) {
        return null;
    }

    const mergedJob = mergeStoredJob(state.jobs[index], update);
    state.jobs[index] = mergedJob;
    return mergedJob;
}

function applyJobDataset(element, job) {
    element.dataset.jobId = getJobId(job);
    element.dataset.status = job?.status || STATUS.QUEUED;
    element.dataset.bpm = String(job?.bpm ?? "");
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

function createPlatformPill(job) {
    const platform = job?.platform || detectPlatform(job?.url || "");
    const label = platformPillLabel(platform);
    if (!label) return null;

    const pill = document.createElement("span");
    pill.className = `platform-pill platform-pill--${platform}`;
    pill.textContent = label;
    pill.title = label;
    return pill;
}

function renderJobTitle(job) {
    const fragment = document.createDocumentFragment();
    const pill = createPlatformPill(job);
    if (pill) {
        fragment.appendChild(pill);
    }

    const titleText = document.createElement("span");
    titleText.className = "job-title-text";
    titleText.textContent = getTitleText(job);
    fragment.appendChild(titleText);
    return fragment;
}

function renderDesktopTitle(job) {
    const td = document.createElement("td");
    td.dataset.label = "Title";
    td.className = "job-title-cell job-title-cell--wide col-title job-title-popover-target";
    td.dataset.popoverText = job?.video_meta_hover || job?.url || "";
    td.append(renderJobTitle(job));
    return td;
}

function formatDesktopMediaPrimary(job) {
    const parts = [getTypeLabel(job), getQualityLabel(job)].filter((part) => part && part !== EMPTY_VALUE);
    return parts.join(" · ") || EMPTY_VALUE;
}

function formatDesktopMediaSecondary(job) {
    const parts = [job?.codec || "", formatBpmCompact(job?.bpm), formatBitrateText(job?.bitrate_kbps)]
        .filter((part) => part && part !== EMPTY_VALUE);
    return parts.join(" · ") || "Live job metadata updates here";
}

function renderDesktopMeta(job) {
    const mediaCell = document.createElement("td");
    mediaCell.dataset.label = "Media";
    mediaCell.className = "td-mono job-meta-cell col-media";

    const mediaValue = document.createElement("span");
    mediaValue.className = "job-media-value";
    mediaValue.textContent = formatDesktopMediaPrimary(job);

    const mediaDetail = document.createElement("div");
    mediaDetail.className = "meta-sub job-media-detail";
    mediaDetail.textContent = formatDesktopMediaSecondary(job);

    mediaCell.append(mediaValue, mediaDetail);

    return [mediaCell];
}

function renderMobileMeta(job) {
    const meta = document.createElement("p");
    meta.className = "job-item__meta jobs-mobile-meta jobs-mobile-media";
    meta.dataset.role = "job-meta-summary";
    meta.textContent = formatMobileMediaLine(job);
    return meta;
}

function renderMobileTime(job) {
    const time = document.createElement("p");
    time.className = "job-item__time";
    time.dataset.role = "job-time";
    const value = formatMobileTimeLine(job?.created_at) || EMPTY_VALUE;
    time.textContent = value;
    time.setAttribute("aria-label", `Added ${value}`);
    return time;
}

function createMobileDetailItem(label, value) {
    const item = document.createElement("div");
    item.className = "job-item__detail";

    const detailLabel = document.createElement("span");
    detailLabel.className = "job-item__detail-label";
    detailLabel.textContent = label;

    const detailValue = document.createElement("span");
    detailValue.className = "job-item__detail-value";
    detailValue.textContent = value;

    item.append(detailLabel, detailValue);
    return item;
}

function renderMobileDetails(job) {
    const details = document.createElement("div");
    details.className = "job-item__details visually-hidden";
    details.dataset.role = "job-details";

    details.append(
        createMobileDetailItem("Codec", job?.codec || EMPTY_VALUE),
        createMobileDetailItem("Bitrate", formatBitrateText(job?.bitrate_kbps)),
        createMobileDetailItem("Added", formatCreatedText(job?.created_at) || EMPTY_VALUE),
    );

    return details;
}

function renderJobStatus(job, { mobile = false, showSize = true } = {}) {
    const container = document.createElement(mobile ? "div" : "td");
    container.dataset.label = "Status";
    container.dataset.role = "job-status";
    container.className = mobile ? "job-item__status" : "col-status";
    container.append(createStatusElement(
        job?.status,
        job?.filesize_bytes,
        job?.progress,
        job?.message,
        { showSize },
    ));
    return container;
}

function renderJobActions(job, { mobile = false } = {}) {
    const container = document.createElement(mobile ? "div" : "td");
    const action = resolveJobAction(job);
    container.dataset.label = "Action";
    container.dataset.role = "job-actions";
    container.dataset.actionCategory = action.category;
    container.dataset.actionKey = action.actionKey;
    container.className = mobile ? "job-item__actions" : "col-actions";

    const actionButton = createActionButton(job);
    if (mobile) {
        container.append(actionButton);
        return container;
    }

    const actionWrap = document.createElement("div");
    actionWrap.className = "action-cell-wrap";
    actionWrap.append(actionButton);
    container.append(actionWrap);
    return container;
}

/**
 * Updates the status CSS classes on an existing job node.
 * @param {HTMLElement} element
 * @param {string} status
 */
function applyRowStatusClasses(element, status) {
    element.classList.toggle(
        "row-done",
        status === STATUS.DONE || status === STATUS.ANALYSIS_DONE,
    );
    element.classList.toggle("row-error", status === STATUS.ERROR);
    element.classList.toggle("row-pending", status !== "" && !TERMINAL_STATUSES.has(String(status || "")));
    element.dataset.status = status;
}

function buildCreatedCell(isoOrFormatted) {
    const td = document.createElement("td");
    td.dataset.label = "Created";
    td.dataset.role = "job-created";
    td.className = "text-nowrap col-date";

    const text = isoOrFormatted == null ? "" : String(isoOrFormatted);
    const dt = parseUtcTimestamp(text);
    if (Number.isNaN(dt.getTime())) {
        td.textContent = text;
        return td;
    }

    const datePart = document.createElement("span");
    datePart.className = "date-part";
    datePart.textContent = DATE_FORMATTER.format(dt);

    const timePart = document.createElement("span");
    timePart.className = "time-part";
    timePart.textContent = TIME_FORMATTER.format(dt);

    td.append(datePart, " ", timePart);
    return td;
}

function buildDesktopEmptyState(message, id = "emptyRow") {
    const row = document.createElement("tr");
    row.id = id;

    const td = document.createElement("td");
    td.colSpan = getJobsTableColumnCount();

    const wrapper = document.createElement("div");
    wrapper.className = "empty-state";

    const iconDiv = document.createElement("span");
    iconDiv.className = "material-symbols-outlined icon";
    iconDiv.setAttribute("aria-hidden", "true");
    iconDiv.textContent = "inbox";

    const p = document.createElement("p");
    p.textContent = message;

    wrapper.append(iconDiv, p);
    td.append(wrapper);
    row.append(td);
    return row;
}

function buildMobileEmptyState(message, id = "jobsEmptyState") {
    const emptyState = document.createElement("div");
    emptyState.id = id;
    emptyState.className = "jobs-mobile-empty empty-state empty-state--mobile";

    const iconDiv = document.createElement("span");
    iconDiv.className = "material-symbols-outlined icon";
    iconDiv.setAttribute("aria-hidden", "true");
    iconDiv.textContent = "inbox";

    const p = document.createElement("p");
    p.textContent = message;

    emptyState.append(iconDiv, p);
    return emptyState;
}

function buildDesktopJob(job) {
    const tr = document.createElement("tr");
    applyJobDataset(tr, job);
    applyRowStatusClasses(tr, job?.status || STATUS.QUEUED);
    tr.append(
        renderDesktopTitle(job),
        ...renderDesktopMeta(job),
        renderJobStatus(job),
        buildCreatedCell(job?.created_at),
        renderJobActions(job),
    );
    return tr;
}

function buildMobileJob(job) {
    const article = document.createElement("article");
    article.className = "job-item jobs-mobile-item jobs-mobile-feed-item";
    applyJobDataset(article, job);
    applyRowStatusClasses(article, job?.status || STATUS.QUEUED);

    const body = document.createElement("div");
    body.className = "job-item__body";

    const titleWrap = document.createElement("div");
    titleWrap.dataset.label = "Title";
    titleWrap.dataset.role = "job-title";
    titleWrap.className = "job-item__title-wrap";
    titleWrap.append(renderJobTitle(job));

    const titleRow = document.createElement("div");
    titleRow.className = "job-item__title-row";
    titleRow.append(titleWrap, renderJobStatus(job, { mobile: true, showSize: false }));

    body.dataset.action = "open-detail";
    body.dataset.jobId = getJobId(job);
    body.setAttribute("role", "button");
    body.setAttribute("tabindex", "0");
    body.setAttribute("aria-label", `View details for ${getTitleText(job) || "this job"}`);
    body.append(titleRow, renderMobileMeta(job), renderMobileTime(job), renderMobileDetails(job));

    const actionWrap = document.createElement("div");
    actionWrap.className = "job-item__primary-action";
    actionWrap.dataset.label = "Action";
    actionWrap.append(createPrimaryActionButton(job));

    article.append(body, actionWrap);
    return article;
}

function patchDesktopJobNode(row, job) {
    applyJobDataset(row, job);
    applyRowStatusClasses(row, job?.status || STATUS.QUEUED);

    const titleCell = row.querySelector(".job-title-cell");
    if (titleCell instanceof HTMLElement) {
        titleCell.dataset.popoverText = job?.video_meta_hover || job?.url || "";
        const titleText = titleCell.querySelector(".job-title-text");
        if (titleText instanceof HTMLElement) {
            titleText.textContent = getTitleText(job);
        }
    }

    const mediaCell = row.querySelector('td[data-label="Media"]');
    if (mediaCell instanceof HTMLElement) {
        const mediaValue = mediaCell.querySelector(".job-media-value");
        if (mediaValue instanceof HTMLElement) {
            mediaValue.textContent = formatDesktopMediaPrimary(job);
        }

        const mediaDetail = mediaCell.querySelector(".job-media-detail");
        if (mediaDetail instanceof HTMLElement) {
            mediaDetail.textContent = formatDesktopMediaSecondary(job);
        }
    }

    const statusCell = row.querySelector('[data-role="job-status"]');
    if (statusCell instanceof HTMLTableCellElement) {
        statusCell.replaceWith(renderJobStatus(job));
    }

    const createdCell = row.querySelector('[data-role="job-created"]');
    if (createdCell instanceof HTMLTableCellElement) {
        createdCell.replaceWith(buildCreatedCell(job?.created_at));
    }

    const actionsCell = row.querySelector('[data-role="job-actions"]');
    if (actionsCell instanceof HTMLTableCellElement) {
        const nextAction = resolveJobAction(job);
        if (actionsCell.dataset.actionKey !== nextAction.actionKey) {
            actionsCell.replaceWith(renderJobActions(job));
        } else {
            actionsCell.dataset.actionCategory = nextAction.category;
            actionsCell.dataset.actionKey = nextAction.actionKey;
        }
    }
}

function patchMobileJobNode(article, job) {
    applyJobDataset(article, job);
    applyRowStatusClasses(article, job?.status || STATUS.QUEUED);

    const titleWrap = article.querySelector('[data-role="job-title"]');
    if (titleWrap instanceof HTMLElement) {
        titleWrap.replaceChildren(renderJobTitle(job));
        titleWrap.setAttribute("aria-label", getTitleText(job));
    }

    const meta = article.querySelector('[data-role="job-meta-summary"]');
    if (meta instanceof HTMLElement) {
        meta.textContent = formatMobileMediaLine(job);
    }

    const time = article.querySelector('[data-role="job-time"]');
    if (time instanceof HTMLElement) {
        const value = formatMobileTimeLine(job?.created_at) || EMPTY_VALUE;
        time.textContent = value;
        time.setAttribute("aria-label", `Added ${value}`);
    }

    const details = article.querySelector('[data-role="job-details"]');
    if (details instanceof HTMLElement) {
        details.replaceWith(renderMobileDetails(job));
    }

    const status = article.querySelector('[data-role="job-status"]');
    if (status instanceof HTMLElement) {
        status.replaceWith(renderJobStatus(job, { mobile: true, showSize: false }));
    }

    const body = article.querySelector(".job-item__body");
    if (body instanceof HTMLElement) {
        body.dataset.jobId = getJobId(job);
        body.setAttribute("aria-label", `View details for ${getTitleText(job) || "this job"}`);
    }

    const actionWrap = article.querySelector(".job-item__primary-action");
    if (actionWrap instanceof HTMLElement) {
        const nextAction = resolveJobAction(job);
        if (actionWrap.dataset.actionKey !== nextAction.actionKey) {
            actionWrap.dataset.actionCategory = nextAction.category;
            actionWrap.dataset.actionKey = nextAction.actionKey;
            actionWrap.replaceChildren(createPrimaryActionButton(job));
        } else {
            actionWrap.dataset.actionCategory = nextAction.category;
            actionWrap.dataset.actionKey = nextAction.actionKey;
        }
    }
}

function renderDesktopJobs() {
    const tbody = getTbody();
    if (!tbody) {
        return;
    }

    if (state.jobs.length === 0) {
        tbody.replaceChildren(buildDesktopEmptyState("No downloads yet. Add a URL above!"));
        state.desktopNodes.clear();
        return;
    }

    const fragment = document.createDocumentFragment();
    const nextNodes = new Map();
    for (const job of state.jobs) {
        const node = buildDesktopJob(job);
        fragment.append(node);
        nextNodes.set(getJobId(job), node);
    }
    tbody.replaceChildren(fragment);
    state.desktopNodes = nextNodes;
}

function renderMobileJobs() {
    const mobileList = getMobileList();
    if (!mobileList) {
        return;
    }

    if (state.jobs.length === 0) {
        mobileList.replaceChildren(buildMobileEmptyState("No downloads yet. Add a URL above!"));
        state.mobileNodes.clear();
        return;
    }

    const fragment = document.createDocumentFragment();
    const nextNodes = new Map();
    for (const job of state.jobs) {
        const node = buildMobileJob(job);
        fragment.append(node);
        nextNodes.set(getJobId(job), node);
    }
    mobileList.replaceChildren(fragment);
    state.mobileNodes = nextNodes;
}

function renderActiveJobs() {
    renderDesktopJobs();
    renderMobileJobs();
    syncSurfaceVisibility();
}

function prependRenderedJobToSurface(container, renderedNodes, job, buildNode) {
    if (!(container instanceof Element)) {
        return;
    }

    const jobId = getJobId(job);
    const existingNode = renderedNodes.get(jobId);
    const nextNode = buildNode(job);

    if (existingNode instanceof HTMLElement) {
        preserveRenderState(existingNode, nextNode);
        renderedNodes.delete(jobId);
        existingNode.remove();
    }

    clearContainerEmptyState(container);
    container.prepend(nextNode);
    renderedNodes.set(jobId, nextNode);
    trimRenderedNodes(container, renderedNodes);
}

function appendRenderedJobsToSurface(container, renderedNodes, jobs, buildNode) {
    if (!(container instanceof Element) || jobs.length === 0) {
        return;
    }

    clearContainerEmptyState(container);

    const fragment = document.createDocumentFragment();
    for (const job of jobs) {
        const node = buildNode(job);
        fragment.append(node);
        renderedNodes.set(getJobId(job), node);
    }

    container.append(fragment);
    trimRenderedNodes(container, renderedNodes);
}

function hasMountedJob(container, renderedNodes, jobId) {
    if (!(container instanceof Element) || !jobId) {
        return false;
    }

    const mappedNode = renderedNodes.get(jobId);
    return mappedNode instanceof HTMLElement
        && mappedNode.isConnected
        && mappedNode.parentElement === container;
}

function prependRenderedJob(job) {
    const tbody = getTbody();
    const mobileList = getMobileList();
    if (!tbody || !mobileList) {
        return;
    }

    if (state.jobs.length > 0 && (state.desktopNodes.size === 0 || state.mobileNodes.size === 0)) {
        renderActiveJobs();
        return;
    }

    prependRenderedJobToSurface(tbody, state.desktopNodes, job, buildDesktopJob);
    prependRenderedJobToSurface(mobileList, state.mobileNodes, job, buildMobileJob);

    const jobId = getJobId(job);
    if (!hasMountedJob(tbody, state.desktopNodes, jobId) || !hasMountedJob(mobileList, state.mobileNodes, jobId)) {
        renderActiveJobs();
    }
}

function appendRenderedJobs(jobs) {
    if (!jobs.length) {
        return;
    }

    const tbody = getTbody();
    const mobileList = getMobileList();
    if (!tbody || !mobileList) {
        return;
    }

    if (state.jobs.length > 0 && (state.desktopNodes.size === 0 || state.mobileNodes.size === 0)) {
        renderActiveJobs();
        return;
    }

    appendRenderedJobsToSurface(tbody, state.desktopNodes, jobs, buildDesktopJob);
    appendRenderedJobsToSurface(mobileList, state.mobileNodes, jobs, buildMobileJob);
}

/**
 * Re-render a single stored job in both mounted renderers.
 * Falls back to a full render when the node maps are out of sync.
 * @param {string} jobId
 * @returns {HTMLElement | null}
 */
export function renderStoredJob(jobId) {
    const job = getStoredJob(jobId);
    if (!job) {
        return null;
    }

    let desktopNode = state.desktopNodes.get(jobId);
    let mobileNode = state.mobileNodes.get(jobId);

    if (!(desktopNode instanceof HTMLTableRowElement) || !(mobileNode instanceof HTMLElement)) {
        renderActiveJobs();
        desktopNode = state.desktopNodes.get(jobId);
        mobileNode = state.mobileNodes.get(jobId);
    }

    if (desktopNode instanceof HTMLTableRowElement) {
        patchDesktopJobNode(desktopNode, job);
    }

    if (mobileNode instanceof HTMLElement) {
        patchMobileJobNode(mobileNode, job);
    }

    return state.mobileView ? mobileNode || null : desktopNode || null;
}

/**
 * Apply the title and status filters using store data as the source of truth.
 * @param {string} query
 * @param {string} statusFilter - all, done, or error
 * @returns {{ totalCount: number, visibleCount: number }}
 */
export function applyStoredJobTitleFilter(query, statusFilter = "all") {
    if (state.desktopNodes.size === 0 && state.mobileNodes.size === 0) {
        return { totalCount: 0, visibleCount: 0 };
    }

    const normalizedQuery = String(query || "").trim().toLowerCase();
    const normalizedStatusFilter = String(statusFilter || "all").trim().toLowerCase();
    let totalCount = 0;
    let visibleCount = 0;
    const changes = [];

    for (const job of state.jobs) {
        const jobId = getJobId(job);
        totalCount += 1;

        const searchableText = [getTitleText(job), job?.url || ""].join(" ").toLowerCase();
        const status = String(job?.status || "").toLowerCase();
        const matchesStatus = normalizedStatusFilter === "all"
            || (normalizedStatusFilter === "done" && (status === STATUS.DONE || status === STATUS.ANALYSIS_DONE))
            || (normalizedStatusFilter === "error" && status === STATUS.ERROR);
        const matchesQuery = normalizedQuery === "" || searchableText.includes(normalizedQuery);
        const shouldHide = !(matchesStatus && matchesQuery);
        for (const nodes of [state.desktopNodes, state.mobileNodes]) {
            const row = nodes.get(jobId);
            if (!(row instanceof HTMLElement)) {
                continue;
            }

            const isHidden = row.classList.contains("d-none");
            if (shouldHide !== isHidden) {
                changes.push({ row, shouldHide });
            }
        }

        if (!shouldHide) {
            visibleCount += 1;
        }
    }

    for (const { row, shouldHide } of changes) {
        row.classList.toggle("d-none", shouldHide);
    }

    return { totalCount, visibleCount };
}

/**
 * Normalize, store, and re-render a job update in one step.
 * @param {object | null | undefined} payload
 * @returns {object | null}
 */
export function applyJobUpdate(payload) {
    const normalizedUpdate = normalizeJobUpdate(payload);
    if (!normalizedUpdate) {
        return null;
    }

    const updatedJob = updateJobStore(normalizedUpdate);
    if (!updatedJob) {
        return null;
    }

    renderStoredJob(updatedJob.id);
    return updatedJob;
}

function dispatchLoadError(error) {
    const detail = error instanceof Error ? error : new Error(String(error));
    window.dispatchEvent(new CustomEvent("jobs-load-error", { detail }));
}

function syncViewMode(force = false) {
    const nextMobileView = mediaQuery?.matches ?? false;
    if (!force && state.mobileView === nextMobileView) {
        return false;
    }

    state.mobileView = nextMobileView;
    syncSurfaceVisibility();
    window.dispatchEvent(new CustomEvent("jobs-layout-change", {
        detail: { mobile: state.mobileView },
    }));
    return true;
}

export function syncMobileJobsList() {
    syncViewMode();
}

function trimJobs() {
    const maxRows = state.preserveHistory ? HARD_MAX_ROWS : CONFIG.MAX_ROWS;
    if (state.jobs.length <= maxRows) {
        return;
    }

    const removedJobs = state.jobs.splice(maxRows);
    for (const job of removedJobs) {
        state.jobIds.delete(getJobId(job));
    }

    if (state.preserveHistory) {
        // `nextOffset` belongs to the server's complete result set. Local
        // trimming must not move it backwards and fetch the same page again.
        state.done = true;
    }
}

function readBootstrapJobs() {
    const node = getBootstrapNode();
    if (!(node instanceof HTMLScriptElement) || !node.textContent) {
        return [];
    }

    try {
        const parsed = JSON.parse(node.textContent);
        if (!Array.isArray(parsed)) {
            return [];
        }

        const seen = new Set();
        const jobs = [];
        for (const job of parsed) {
            if (typeof job !== "object" || job == null || job.id == null) {
                continue;
            }

            const jobId = getJobId(job);
            if (!jobId || seen.has(jobId)) {
                continue;
            }
            seen.add(jobId);
            jobs.push(job);
        }
        return jobs;
    } catch (error) {
        reportError(error, {
            module: "jobs",
            action: "readBootstrapJobs",
        });
        return [];
    }
}

/**
 * Prepends a job to the active collection, replacing any existing entry.
 * @param {object} job
 */
export function prependJob(job) {
    const jobId = getJobId(job);
    if (!jobId) {
        return;
    }

    const jobsBeforePrepend = state.jobs.length;
    const existingIndex = state.jobs.findIndex((item) => getJobId(item) === jobId);
    const existed = existingIndex !== -1;
    if (existed) {
        state.jobs.splice(existingIndex, 1);
    }

    state.jobs.unshift(job);
    state.jobIds.add(jobId);
    state.done = false;
    trimJobs();
    prependRenderedJob(job);

    if (!existed && (state.preserveHistory || jobsBeforePrepend < CONFIG.MAX_ROWS)) {
        state.nextOffset += 1;
    }
}

/**
 * Insert a full job snapshot into the current store/render pipeline.
 * Unknown jobs are surfaced at the top of the active list.
 * @param {object | null | undefined} job
 * @returns {object | null}
 */
export function upsertJobSnapshot(job) {
    const jobId = getJobId(job);
    if (!jobId) {
        return null;
    }

    prependJob(job);
    return getStoredJob(jobId);
}

/**
 * Resets pagination state so scroll-loading restarts from the beginning.
 * Call this after a full reload or filter change.
 */
export function resetPagingState() {
    state.done = false;
    state.preserveHistory = false;
    state.nextOffset = 0;
}

/**
 * Fetch and append the next page of jobs to the collection.
 * Dispatches `jobs-load-error` on non-abort failures.
 * @param {(offset: number) => Promise<object[]>} fetchFn
 * @returns {Promise<void>}
 */
export async function loadMore(fetchFn) {
    if (state.loading || state.done) {
        return;
    }

    state.loading = true;
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

        state.preserveHistory = true;
        state.nextOffset = currentOffset + jobs.length;

        const previousLength = state.jobs.length;
        let didAppend = false;

        for (const job of jobs) {
            const jobId = getJobId(job);
            if (!jobId || state.jobIds.has(jobId)) {
                continue;
            }

            state.jobIds.add(jobId);
            state.jobs.push(job);
            didAppend = true;
        }

        if (didAppend) {
            trimJobs();
            appendRenderedJobs(state.jobs.slice(previousLength));
        }

        if (jobs.length < CONFIG.PAGE_SIZE) {
            state.done = true;
        }
    } catch (error) {
        if (error?.name === "AbortError") {
            return;
        }
        reportError(error, {
            module: "jobs",
            action: "loadMore",
            offset: currentOffset,
        });
        dispatchLoadError(error);
    } finally {
        state.loading = false;
    }
}

function init() {
    if (state.initialized || !getTbody() || !getMobileList()) {
        return;
    }

    replaceJobs(readBootstrapJobs());
    state.nextOffset = state.jobs.length;
    trimJobs();
    renderActiveJobs();
    syncViewMode(true);

    mediaQuery?.addEventListener("change", () => {
        syncViewMode(true);
    });

    state.initialized = true;
    window.removeEventListener("jobs-reload", resetPagingState);
    window.addEventListener("jobs-reload", resetPagingState);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
