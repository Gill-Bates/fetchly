//
// app/static/js/ui.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { humanSize } from "./utils.js";

const STATUS_META = {
    analysis: { color: "primary", label: "analysis" },
    analysis_done: { color: "success", label: "done" },
    done:  { color: "success", label: "done" },
    error: { color: "danger", label: "error" },
};

const READY_STATUSES = new Set(["done", "analysis", "analysis_done"]);

/** @type {boolean} */
const lalalEnabled = (globalThis.document?.documentElement.dataset.lalalEnabled ?? "false") === "true";

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

    const normalizedStatus = status || "queued";
    const meta = STATUS_META[normalizedStatus] || { color: "primary", label: normalizedStatus };

    const pill = document.createElement("span");
    pill.className = `status-pill status-pill-${meta.color}`;
    pill.textContent = meta.label;

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
    link.className = "btn btn-primary btn-sm btn-icon";
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
    button.setAttribute("aria-label", title);
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
    wrapper.className = "action-buttons";

    if (READY_STATUSES.has(status || "")) {
        wrapper.appendChild(createDownloadLink(jobId));

        if (jobType === "audio" && lalalEnabled) {
            const dropdown = document.createElement("div");
            dropdown.className = "dropdown dropup";

            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = "btn btn-sm btn-icon btn-outline-secondary action-menu-toggle";
            toggle.dataset.bsToggle = "dropdown";
            toggle.dataset.bsBoundary = "viewport";
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute("aria-label", "More actions");
            toggle.appendChild(icon("more_vert"));

            const menu = document.createElement("ul");
            menu.className = "dropdown-menu dropdown-menu-end";

            // Instrumental item
            const liInst = document.createElement("li");
            const btnInst = document.createElement("button");
            btnInst.type = "button";
            btnInst.className = "dropdown-item";
            btnInst.dataset.action = "lalal-split";
            btnInst.dataset.jobId = jobId;
            btnInst.dataset.stem = "instrumental";
            btnInst.setAttribute("aria-label", "Download Instrumental");
            btnInst.appendChild(icon("music_off"));
            btnInst.appendChild(document.createTextNode(" Instrumental"));
            liInst.appendChild(btnInst);

            // Vocals item
            const liVocals = document.createElement("li");
            const btnVocals = document.createElement("button");
            btnVocals.type = "button";
            btnVocals.className = "dropdown-item";
            btnVocals.dataset.action = "lalal-split";
            btnVocals.dataset.jobId = jobId;
            btnVocals.dataset.stem = "vocals";
            btnVocals.setAttribute("aria-label", "Download A Cappella");
            btnVocals.appendChild(icon("mic"));
            btnVocals.appendChild(document.createTextNode(" A Cappella"));
            liVocals.appendChild(btnVocals);

            menu.append(liInst, liVocals);
            dropdown.append(toggle, menu);
            wrapper.appendChild(dropdown);
            return wrapper;
        }

        return wrapper;
    }

    if (["queued", "processing", "downloading", "transcoding"].includes(status || "")) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-danger btn-sm btn-icon";
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
