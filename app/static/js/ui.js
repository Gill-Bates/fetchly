//
// app/static/js/ui.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CANCELLABLE_STATUSES, DOWNLOADABLE_STATUSES, LALAL_MAX_DURATION_MINUTES, LALAL_MAX_DURATION_SECONDS } from "./config.js";
import { EMPTY_VALUE, humanSize } from "./utils.js";

export const ACTION_CATEGORY = Object.freeze({
    DOWNLOAD: "download",
    CANCEL: "cancel",
    DETAIL: "detail",
});

export const STATUS_META = Object.freeze({
    analysis: { color: "primary", label: "Analyzing" },
    analysis_done: { color: "success", label: "Done" },
    cancelled: { color: "secondary", label: "Cancelled" },
    done: { color: "success", label: "Done" },
    downloading: { color: "primary", label: "Running" },
    error: { color: "danger", label: "Error" },
    processing: { color: "primary", label: "Running" },
    queued: { color: "primary", label: "Queued" },
    transcoding: { color: "primary", label: "Running" },
});

const PROGRESS_STATUSES = new Set(["analysis", "downloading", "processing", "transcoding"]);

export function getStatusMeta(status) {
    const normalizedStatus = status || "queued";
    return STATUS_META[normalizedStatus] || {
        color: "primary",
        label: String(normalizedStatus).toUpperCase(),
    };
}

export function getStatusText(status, progress = null) {
    const normalizedStatus = status || "queued";
    const meta = getStatusMeta(normalizedStatus);
    const parsedProgress = Number(progress);

    if (PROGRESS_STATUSES.has(normalizedStatus) && Number.isFinite(parsedProgress) && parsedProgress >= 0) {
        return `${meta.label} ${Math.round(parsedProgress)}%`;
    }

    return meta.label;
}

export function getStatusPillClass(status) {
    return `status-pill status-pill-${getStatusMeta(status).color}`;
}

const LALAL_STEMS = Object.freeze([
    Object.freeze({ icon: "music_off", label: "Instrumental", stem: "instrumental" }),
    Object.freeze({ icon: "mic", label: "A Cappella", stem: "vocals" }),
]);

export function isLalalEnabled() {
    return document.documentElement.dataset.lalalEnabled === "true";
}

export function isLalalDurationGuardEnabled() {
    return document.documentElement.dataset.lalalDurationGuard !== "false";
}

function getKnownDurationSeconds(job) {
    const rawDuration = job?.duration_seconds;
    if (rawDuration == null || String(rawDuration).trim() === "") {
        return null;
    }

    const durationSeconds = Number(rawDuration);
    return Number.isFinite(durationSeconds) && durationSeconds >= 0 ? durationSeconds : null;
}

export function isDurationBlocked(job) {
    if (!isLalalDurationGuardEnabled()) {
        return false;
    }
    const durationSeconds = getKnownDurationSeconds(job);
    return durationSeconds === null || durationSeconds > LALAL_MAX_DURATION_SECONDS;
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

function appendAudioDownloadActions(menu, job) {
    const jobId = getJobId(job);
    const lalalEnabled = isLalalEnabled();
    const durationBlocked = isDurationBlocked(job);

    menu.appendChild(createDivider());
    menu.appendChild(createDropdownButton("content_cut", "Trim", { action: "open-trim", jobId }));

    menu.appendChild(createDivider());
    for (const { icon: iconName, label, stem } of LALAL_STEMS) {
        const isDisabled = !lalalEnabled || durationBlocked;
        let title = "";
        if (!lalalEnabled) {
            title = "Lalal.ai is not connected";
        } else if (durationBlocked) {
            title = getKnownDurationSeconds(job) === null
                ? "Track duration unknown — blocked by Duration Guard"
                : `Track exceeds ${LALAL_MAX_DURATION_MINUTES} min — blocked by Duration Guard`;
        }
        menu.appendChild(createDropdownButton(
            iconName,
            label,
            { action: "lalal-split", jobId, stem },
            {
                trailingNode: createProviderLogo(),
                disabled: isDisabled,
                title,
            },
        ));
    }
}

function createDownloadOptionsToggle(className) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = className;
    toggle.dataset.bsToggle = "dropdown";
    toggle.setAttribute("aria-haspopup", "menu");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "More download options");
    toggle.title = "More download options";

    const srText = document.createElement("span");
    srText.className = "visually-hidden";
    srText.textContent = "More download options";
    toggle.appendChild(srText);
    return toggle;
}

function createDownloadOptionsMenu(job, downloadHref) {
    const jobId = getJobId(job);
    const jobType = job?.type || "";
    const menu = document.createElement("ul");
    menu.className = "dropdown-menu dropdown-menu-end";
    menu.appendChild(createDropdownLink("download", "Download", downloadHref, { download: true }));
    menu.appendChild(createDropdownButton(
        "share",
        jobType === "audio" ? "Share Audio" : "Share Video",
        { action: "share-job", jobId },
    ));

    if (jobType === "audio") {
        appendAudioDownloadActions(menu, job);
    }
    return menu;
}

function createDesktopDownloadAction(job) {
    const jobId = getJobId(job);

    const btnGroup = document.createElement("div");
    btnGroup.className = "btn-group";

    const downloadBtn = createDownloadLink("btn btn-primary btn-sm btn-icon", jobId);
    const toggle = createDownloadOptionsToggle(
        "btn btn-primary btn-sm dropdown-toggle dropdown-toggle-split",
    );
    const menu = createDownloadOptionsMenu(job, downloadBtn.href);

    btnGroup.append(downloadBtn, toggle, menu);
    return btnGroup;
}

function createMobileDownloadAction(job) {
    const downloadBtn = createDownloadLink(
        "btn jobs-mobile-action jobs-mobile-action--download",
        getJobId(job),
    );
    const toggle = createDownloadOptionsToggle(
        "btn jobs-mobile-action jobs-mobile-action--menu dropdown-toggle",
    );
    const menu = createDownloadOptionsMenu(job, downloadBtn.href);

    const btnGroup = document.createElement("div");
    btnGroup.className = "btn-group jobs-mobile-action-group";
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
    const knownDuration = getKnownDurationSeconds(job);
    const durationBucket = knownDuration === null
        ? "unknown"
        : knownDuration > LALAL_MAX_DURATION_SECONDS ? "long" : "ok";
    const lalalState = job?.type === "audio"
        ? `${isLalalEnabled() ? "lalal-on" : "lalal-off"}:${isLalalDurationGuardEnabled() ? "guard-on" : "guard-off"}`
        : "none";
    const actionKey = `${category}:${job?.type || ""}:${durationBucket}:${lalalState}`;
    return {
        category,
        actionKey,
        jobId,
        job,
    };
}

/**
 * Build a status pill, optional file-size badge, and an error tooltip.
 * Returns a <div class="status-inline">, which events.js relies on for live updates.
 * @param {string} status
 * @param {number | null | undefined} sizeBytes
 * @param {number | null | undefined} progress
 * @param {string | null | undefined} message
 * @returns {HTMLDivElement} Element with class "status-inline"
 */
export function createStatusElement(status, sizeBytes, progress = null, message = null, { showSize = true } = {}) {
    const wrapper = document.createElement("div");
    wrapper.className = "status-inline";

    const pill = document.createElement("span");
    pill.className = getStatusPillClass(status);
    const statusText = getStatusText(status, progress);
    pill.textContent = statusText;

    wrapper.appendChild(pill);

    const renderedSize = humanSize(sizeBytes);
    if (showSize && renderedSize !== EMPTY_VALUE) {
        const size = document.createElement("span");
        size.className = "status-size";
        size.setAttribute("title", "File size");
        size.textContent = renderedSize;
        wrapper.appendChild(size);
    }

    const normalizedMessage = typeof message === "string" ? message.trim() : "";
    if (normalizedStatus(status) === "error" && normalizedMessage) {
        pill.title = normalizedMessage;
        pill.setAttribute("aria-label", `${statusText}: ${normalizedMessage}`);
    }

    return wrapper;
}

function normalizedStatus(status) {
    return String(status || "queued").trim().toLowerCase();
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
 * Build the primary action used by the dense mobile jobs list. Downloadable
 * jobs use a compact split action so Download stays one tap away while Share
 * and the remaining media actions are available from the adjacent menu.
 * @param {object} job
 * @returns {HTMLAnchorElement | HTMLButtonElement | HTMLDivElement}
 */
export function createPrimaryActionButton(job) {
    return renderPrimaryAction(resolveJobAction(job));
}

function renderPrimaryAction(action) {
    if (action.category === ACTION_CATEGORY.DOWNLOAD) {
        return createMobileDownloadAction(action.job);
    }

    if (action.category === ACTION_CATEGORY.CANCEL) {
        return createCancelAction(action.job, "btn jobs-mobile-action");
    }

    return createDetailAction(action.job, "btn jobs-mobile-action");
}
