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
        // Single dropdown with download toggle - matches _action_btn.html template
        const dropdown = document.createElement("div");
        dropdown.className = "dropdown";

        const toggle = document.createElement("a");
        toggle.href = `/download/${encodeURIComponent(jobId)}`;
        toggle.className = "btn btn-primary btn-sm btn-icon dropdown-toggle";
        toggle.dataset.bsToggle = "dropdown";
        toggle.dataset.bsBoundary = "viewport";
        toggle.setAttribute("title", "Download");
        toggle.setAttribute("aria-label", "Download");
        toggle.setAttribute("role", "button");
        toggle.appendChild(icon("download"));

        const menu = document.createElement("ul");
        menu.className = "dropdown-menu dropdown-menu-end";

        // Download item
        const liDownload = document.createElement("li");
        const linkDownload = document.createElement("a");
        linkDownload.className = "dropdown-item";
        linkDownload.href = `/download/${encodeURIComponent(jobId)}`;
        linkDownload.appendChild(icon("download"));
        linkDownload.appendChild(document.createTextNode(" Download"));
        liDownload.appendChild(linkDownload);
        menu.appendChild(liDownload);

        // Lalal.ai options for audio jobs
        if (jobType === "audio" && lalalEnabled) {
            const divider = document.createElement("li");
            divider.innerHTML = '<hr class="dropdown-divider">';
            menu.appendChild(divider);

            // Instrumental item
            const liInst = document.createElement("li");
            const btnInst = document.createElement("button");
            btnInst.type = "button";
            btnInst.className = "dropdown-item";
            btnInst.dataset.action = "lalal-split";
            btnInst.dataset.jobId = jobId;
            btnInst.dataset.stem = "instrumental";
            btnInst.appendChild(icon("music_off"));
            btnInst.appendChild(document.createTextNode(" Instrumental"));
            liInst.appendChild(btnInst);
            menu.appendChild(liInst);

            // Vocals item
            const liVocals = document.createElement("li");
            const btnVocals = document.createElement("button");
            btnVocals.type = "button";
            btnVocals.className = "dropdown-item";
            btnVocals.dataset.action = "lalal-split";
            btnVocals.dataset.jobId = jobId;
            btnVocals.dataset.stem = "vocals";
            btnVocals.appendChild(icon("mic"));
            btnVocals.appendChild(document.createTextNode(" A Cappella"));
            liVocals.appendChild(btnVocals);
            menu.appendChild(liVocals);
        }

        dropdown.append(toggle, menu);
        wrapper.appendChild(dropdown);
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
