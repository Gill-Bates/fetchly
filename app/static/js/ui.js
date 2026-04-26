//
// app/static/js/ui.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { humanSize } from "./utils.js";

function icon(name) {
    const span = document.createElement("span");
    span.className = "material-symbols-outlined icon-inline";
    span.setAttribute("aria-hidden", "true");
    span.textContent = name;
    return span;
}

export function createStatusElement(status, sizeBytes) {
    const wrapper = document.createElement("div");
    wrapper.className = "status-inline";

    const pill = document.createElement("span");
    const color = status === "error" ? "danger" : status === "done" ? "success" : "primary";
    pill.className = `status-pill status-pill-${color}`;
    pill.appendChild(icon(status === "done" ? "check_circle" : status === "error" ? "error" : "schedule"));
    pill.appendChild(document.createTextNode(` ${status || "queued"}`));

    wrapper.appendChild(pill);

    const renderedSize = humanSize(sizeBytes);
    if (renderedSize !== "-") {
        const size = document.createElement("span");
        size.className = "status-size";
        size.setAttribute("title", "Dateigroesse");
        size.textContent = renderedSize;
        wrapper.appendChild(size);
    }

    return wrapper;
}

export function createActionButton(jobId, status) {
    if (status === "done") {
        const link = document.createElement("a");
        link.href = `/download/${jobId}`;
        link.className = "btn btn-download btn-sm";
        link.setAttribute("title", "Download");
        link.setAttribute("aria-label", "Download");
        link.appendChild(icon("download"));
        return link;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-details btn-sm btn-outline-secondary";
    button.dataset.action = "open-detail";
    button.dataset.jobId = jobId;
    button.setAttribute("title", "Details");
    button.setAttribute("aria-label", "Details");
    button.appendChild(icon("info"));
    return button;
}