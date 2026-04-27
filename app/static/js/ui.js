//
// app/static/js/ui.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { humanSize } from "./utils.js";

const STATUS_META = {
    done: { color: "success", icon: "check_circle" },
    error: { color: "danger", icon: "error" },
};

/** @type {boolean} */
const lalalEnabled = Boolean(globalThis.window?.lalalEnabled);

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

/**
 * Build a status pill and optional file-size badge.
 * @param {string} status
 * @param {number | null | undefined} sizeBytes
 * @returns {HTMLDivElement}
 */
export function createStatusElement(status, sizeBytes) {
    const wrapper = document.createElement("div");
    wrapper.className = "status-inline";

    const meta = STATUS_META[status] || { color: "primary", icon: "schedule" };

    const pill = document.createElement("span");
    pill.className = `status-pill status-pill-${meta.color}`;
    pill.appendChild(icon(meta.icon));
    pill.appendChild(document.createTextNode(` ${status || "queued"}`));

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
 * Create a download anchor for a completed job.
 * @param {string} jobId
 * @returns {HTMLAnchorElement}
 */
function createDownloadLink(jobId) {
    const link = document.createElement("a");
    link.href = `/download/${encodeURIComponent(jobId)}`;
    link.className = "btn btn-download btn-sm";
    link.setAttribute("title", "Download");
    link.setAttribute("aria-label", "Download");
    link.appendChild(icon("download"));
    return link;
}

/**
 * Create a Lalal.ai stem-split button.
 * @param {string} jobId
 * @param {string} stem
 * @param {string} title
 * @param {string} iconName
 * @param {string} colorClass
 * @returns {HTMLButtonElement}
 */
function createLalalButton(jobId, stem, title, iconName, colorClass) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `btn btn-sm ${colorClass} btn-lalal`;
    button.dataset.action = "lalal-split";
    button.dataset.jobId = jobId;
    button.dataset.stem = stem;
    button.setAttribute("title", title);
    button.appendChild(icon(iconName));
    return button;
}

/**
 * Build the action-cell content for a job row.
 * @param {string} jobId
 * @param {string} status
 * @param {string | undefined} jobType
 * @returns {HTMLDivElement}
 */
export function createActionButton(jobId, status, jobType) {
    const wrapper = document.createElement("div");
    wrapper.className = "d-flex gap-1 align-items-center";

    if (status === "done") {
        wrapper.appendChild(createDownloadLink(jobId));

        // Add Lalal.ai buttons only for audio jobs
        if (jobType === "audio" && lalalEnabled) {
            wrapper.appendChild(createLalalButton(jobId, "instrumental", "Download Instrumental", "music_off", "btn-outline-info"));
            wrapper.appendChild(createLalalButton(jobId, "vocals", "Download A Cappella", "mic", "btn-outline-warning"));
        }

        return wrapper;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-details btn-sm btn-outline-secondary";
    button.dataset.action = "open-detail";
    button.dataset.jobId = jobId;
    button.setAttribute("title", "Details");
    button.setAttribute("aria-label", "Details");
    button.appendChild(icon("info"));
    wrapper.appendChild(button);
    return wrapper;
}