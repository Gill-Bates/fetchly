//
// app/static/js/ui.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CANCELLABLE_STATUSES, DOWNLOADABLE_STATUSES } from "./config.js";
import { humanSize } from "./utils.js";

const STATUS_META = Object.freeze({
    analysis: { color: "primary", label: "ANALYSIS" },
    analysis_done: { color: "success", label: "DONE" },
    cancelled: { color: "secondary", label: "CANCELLED" },
    done:  { color: "success", label: "DONE" },
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

const LALAL_ENABLED = document.documentElement.dataset.lalalEnabled === "true";

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

/**
 * Resolve the action button category for a given job status.
 * @param {string} status
 * @returns {"download" | "cancel" | "detail"}
 */
export function getActionButtonCategory(status) {
    const normalizedStatus = status || "";
    if (DOWNLOADABLE_STATUSES.has(normalizedStatus)) {
        return "download";
    }
    if (CANCELLABLE_STATUSES.has(normalizedStatus)) {
        return "cancel";
    }
    return "detail";
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
 * @param {string} jobId
 * @param {string} status
 * @param {string} [jobType]
 * @returns {HTMLDivElement} Wrapper containing the appropriate action controls
 */
export function createActionButton(jobId, status, jobType) {
    const wrapper = document.createElement("div");
    wrapper.className = "action-buttons";

    const actionCategory = getActionButtonCategory(status);
    wrapper.dataset.actionCategory = actionCategory;
    const downloadHref = `/download/${encodeURIComponent(jobId)}`;

    if (actionCategory === "download") {
        // Split button: direct download link + dropdown toggle for more options
        const btnGroup = document.createElement("div");
        btnGroup.className = "btn-group";

        // Primary download button (clickable)
        const downloadBtn = document.createElement("a");
        downloadBtn.href = downloadHref;
        downloadBtn.className = "btn btn-primary btn-sm btn-icon";
        downloadBtn.setAttribute("title", "Download");
        downloadBtn.setAttribute("aria-label", "Download");
        downloadBtn.setAttribute("download", "");
        downloadBtn.appendChild(icon("download"));

        // Separate dropdown toggle
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

        // Download item
        menu.appendChild(createDropdownLink("download", "Download", downloadHref, { download: true }));

        // Trim option for audio jobs (always available)
        if (jobType === "audio") {
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
                        disabled: !LALAL_ENABLED,
                        title: LALAL_ENABLED ? "" : "Lalal.ai is not connected",
                    },
                ));
            }
        }

        btnGroup.append(downloadBtn, toggle, menu);
        wrapper.appendChild(btnGroup);
        return wrapper;
    }

    if (actionCategory === "cancel") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-secondary btn-sm btn-icon";
        button.dataset.action = "cancel-job";
        button.dataset.jobId = jobId;
        button.setAttribute("aria-label", "Cancel");
        button.appendChild(icon("cancel"));
        wrapper.appendChild(button);
        return wrapper;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm btn-outline-secondary btn-icon";
    button.dataset.action = "open-detail";
    button.dataset.jobId = jobId;
    button.setAttribute("aria-label", "Details");
    button.appendChild(icon("info"));
    wrapper.appendChild(button);
    return wrapper;
}
