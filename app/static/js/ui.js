//
// app/static/js/ui.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CANCELLABLE_STATUSES, DOWNLOADABLE_STATUSES } from "./config.js";
import { humanSize } from "./utils.js";

export const ACTION_CATEGORY = Object.freeze({
    DOWNLOAD: "download",
    CANCEL: "cancel",
    DETAIL: "detail",
});

const STATUS_META = Object.freeze({
    analysis: { color: "primary", label: "ANALYSIS" },
    analysis_done: { color: "success", label: "DONE" },
    cancelled: { color: "secondary", label: "CANCELLED" },
    done: { color: "success", label: "DONE" },
    downloading: { color: "primary", label: "DOWNLOADING" },
    error: { color: "danger", label: "ERROR" },
    processing: { color: "primary", label: "PROCESSING" },
    queued: { color: "primary", label: "QUEUED" },
    transcoding: { color: "primary", label: "TRANSCODING" },
});

const LALAL_STEMS = Object.freeze([
    Object.freeze({ icon: "music_off", label: "Instrumental", stem: "instrumental" }),
    Object.freeze({ icon: "mic", label: "A Cappella", stem: "vocals" }),
]);

function isLalalEnabled() {
    return document.documentElement.dataset.lalalEnabled === "true";
}

export function buildDownloadUrl(jobId) {
    return `/download/${encodeURIComponent(jobId)}`;
}

function getJobId(job) {
    return String(job?.id ?? "");
}

function createButton(className, ariaLabel, iconName, {
    title = "",
    dataset = null,
} = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.setAttribute("aria-label", ariaLabel);
    if (title) {
        button.title = title;
    }
    if (dataset) {
        Object.assign(button.dataset, dataset);
    }
    button.appendChild(icon(iconName));
    return button;
}

function createDownloadLink(className, jobId, {
    iconName = "download",
    title = "Download",
    ariaLabel = "Download",
} = {}) {
    const link = document.createElement("a");
    link.href = buildDownloadUrl(jobId);
    link.className = className;
    link.setAttribute("download", "");
    link.setAttribute("aria-label", ariaLabel);
    link.title = title;
    link.appendChild(icon(iconName));
    return link;
}

/**
 * Create a Material Symbols icon span.
 * @param {string} name
 * @returns {HTMLSpanElement}
 */
function icon(name) {
    const span = document.createElement("span");
    span.className = "material-symbols-outlined icon-inline";
    span.setAttribute("aria-hidden", "true");
    span.textContent = name;
    return span;
}

function createProviderLogo() {
    const image = document.createElement("img");
    image.className = "dropdown-provider-logo";
    image.src = "/static/img/lalal_ai_small.svg";
    image.alt = "Lalal.ai";
    image.width = 34;
    image.height = 10;
    return image;
}

function createDivider() {
    const item = document.createElement("li");
    item.setAttribute("role", "separator");

    const divider = document.createElement("hr");
    divider.className = "dropdown-divider";
    item.appendChild(divider);

    return item;
}

function appendDropdownContent(element, iconName, label, trailingNode = null) {
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    if (trailingNode) {
        element.append(icon(iconName), labelEl, trailingNode);
        return;
    }

    element.append(icon(iconName), labelEl);
}

function createDropdownLink(iconName, label, href, { download = false } = {}) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.className = "dropdown-item";
    link.href = href;
    if (download) {
        link.setAttribute("download", "");
    }
    appendDropdownContent(link, iconName, label);
    item.appendChild(link);
    return item;
}

function createDropdownButton(iconName, label, dataset, {
    trailingNode = null,
    disabled = false,
    title = "",
} = {}) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "dropdown-item";
    if (disabled) {
        button.disabled = true;
        button.classList.add("disabled");
        button.setAttribute("aria-disabled", "true");
    }
    if (title) {
        button.title = title;
    }
    Object.assign(button.dataset, dataset);
    appendDropdownContent(button, iconName, label, trailingNode);
    item.appendChild(button);
    return item;
}

function appendAudioDownloadActions(menu, jobId) {
    const lalalEnabled = isLalalEnabled();

    menu.appendChild(createDivider());
    menu.appendChild(createDropdownButton("content_cut", "Trim", { action: "open-trim", jobId }));

    menu.appendChild(createDivider());
    for (const { icon: iconName, label, stem } of LALAL_STEMS) {
        menu.appendChild(createDropdownButton(
            iconName,
            label,
            { action: "lalal-split", jobId, stem },
            {
                trailingNode: createProviderLogo(),
                disabled: !lalalEnabled,
                title: lalalEnabled ? "" : "Lalal.ai is not connected",
            },
        ));
    }
}

function createDesktopDownloadAction(job) {
    const jobId = getJobId(job);
    const jobType = job?.type || "";

    const btnGroup = document.createElement("div");
    btnGroup.className = "btn-group";

    const downloadBtn = createDownloadLink("btn btn-primary btn-sm btn-icon", jobId);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "btn btn-primary btn-sm dropdown-toggle dropdown-toggle-split";
    toggle.dataset.bsToggle = "dropdown";
    toggle.setAttribute("aria-haspopup", "menu");
    toggle.setAttribute("aria-expanded", "false");

    const srText = document.createElement("span");
    srText.className = "visually-hidden";
    srText.textContent = "More options";
    toggle.appendChild(srText);

    const menu = document.createElement("ul");
    menu.className = "dropdown-menu dropdown-menu-end";
    menu.appendChild(createDropdownLink("download", "Download", downloadBtn.href, { download: true }));

    if (jobType === "audio") {
        appendAudioDownloadActions(menu, jobId);
    }

    btnGroup.append(downloadBtn, toggle, menu);
    return btnGroup;
}

function createCancelAction(job, className) {
    return createButton(className, "Cancel", "cancel", {
        title: "Cancel",
        dataset: { action: "cancel-job", jobId: getJobId(job) },
    });
}

function createDetailAction(job, className) {
    return createButton(className, "Details", "info", {
        title: "Details",
        dataset: { action: "open-detail", jobId: getJobId(job) },
    });
}

/**
 * Resolve the action button category for a given job status.
 * @param {string} status
 * @returns {"download" | "cancel" | "detail"}
 */
export function getActionButtonCategory(status) {
    const normalizedStatus = status || "";
    if (DOWNLOADABLE_STATUSES.has(normalizedStatus)) {
        return ACTION_CATEGORY.DOWNLOAD;
    }
    if (CANCELLABLE_STATUSES.has(normalizedStatus)) {
        return ACTION_CATEGORY.CANCEL;
    }
    return ACTION_CATEGORY.DETAIL;
}

export function resolveJobAction(job) {
    const category = getActionButtonCategory(job?.status);
    const jobId = getJobId(job);
    const actionKey = `${category}:${job?.type || ""}`;
    return {
        category,
        actionKey,
        jobId,
        job,
    };
}

/**
 * Build a status pill and optional file-size badge.
 * Returns a <div class="status-inline">, which events.js relies on for live updates.
 * @param {string} status
 * @param {number | null | undefined} sizeBytes
 * @param {number | null | undefined} progress
 * @returns {HTMLDivElement} Element with class "status-inline"
 */
export function createStatusElement(status, sizeBytes, progress = null) {
    const wrapper = document.createElement("div");
    wrapper.className = "status-inline";

    const normalizedStatus = status || "queued";
    const meta = STATUS_META[normalizedStatus] || { color: "primary", label: normalizedStatus.toUpperCase() };

    const pill = document.createElement("span");
    pill.className = `status-pill status-pill-${meta.color}`;
    const parsedProgress = Number(progress);
    if (normalizedStatus === "transcoding" && Number.isFinite(parsedProgress) && parsedProgress >= 0) {
        pill.textContent = `${meta.label} ${Math.round(parsedProgress)}%`;
    } else {
        pill.textContent = meta.label;
    }

    wrapper.appendChild(pill);

    const renderedSize = humanSize(sizeBytes);
    if (renderedSize !== "-") {
        const size = document.createElement("span");
        size.className = "status-size";
        size.setAttribute("title", "File size");
        size.textContent = renderedSize;
        wrapper.appendChild(size);
    }

    return wrapper;
}

/**
 * Build the action-cell content for a job row.
 *
 * Returns one of three variants:
 * - download split button with dropdown when the job is downloadable
 * - cancel button while the job is actively running
 * - detail button for all remaining states
 *
 * Audio downloads add Trim plus Lalal stem-split actions.
 * Lalal entries remain visible when unavailable and are rendered disabled
 * based on <html data-lalal-enabled>.
 * Sets `data-action-category` on the wrapper for live-update handlers.
 *
 * @param {object} job
 * @returns {HTMLDivElement} Wrapper containing the appropriate action controls
 */
export function createActionButton(job) {
    return renderDesktopAction(resolveJobAction(job));
}

function renderDesktopAction(action) {
    const wrapper = document.createElement("div");
    wrapper.className = "action-buttons";

    wrapper.dataset.actionCategory = action.category;
    wrapper.dataset.actionKey = action.actionKey;

    if (action.category === ACTION_CATEGORY.DOWNLOAD) {
        wrapper.appendChild(createDesktopDownloadAction(action.job));
        return wrapper;
    }

    if (action.category === ACTION_CATEGORY.CANCEL) {
        wrapper.appendChild(createCancelAction(action.job, "btn btn-secondary btn-sm btn-icon"));
        return wrapper;
    }

    wrapper.appendChild(createDetailAction(action.job, "btn btn-sm btn-outline-secondary btn-icon"));
    return wrapper;
}

/**
 * Build the single primary action used by the dense mobile jobs list.
 * Downloadable jobs get a direct download action, running jobs get cancel,
 * and all remaining states expose details only.
 * @param {object} job
 * @returns {HTMLAnchorElement | HTMLButtonElement}
 */
export function createPrimaryActionButton(job) {
    return renderPrimaryAction(resolveJobAction(job));
}

function renderPrimaryAction(action) {
    if (action.category === ACTION_CATEGORY.DOWNLOAD) {
        if (action.job?.type === "audio") {
            return createDesktopDownloadAction(action.job);
        }
        return createDownloadLink("btn jobs-mobile-action", action.jobId);
    }

    if (action.category === ACTION_CATEGORY.CANCEL) {
        return createCancelAction(action.job, "btn jobs-mobile-action");
    }

    return createDetailAction(action.job, "btn jobs-mobile-action");
}
