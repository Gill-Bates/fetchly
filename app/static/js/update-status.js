//
// app/static/js/update-status.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module update-status
 * Renders the update-check result of one Settings → System component cell.
 * A cell holds an `[data-update-badge]` link (update available) and a
 * `[data-update-status]` badge (checking / current / unknown). Both use the
 * same pill geometry; only the colour tokens differ per state.
 */

/** Icon + label of every badge state; shared by the version rows and the fetchly tile. */
export const UPDATE_STATE_VIEW = {
    checking: { icon: "sync", text: "Checking…" },
    current: { icon: "check_circle", text: "Up to date" },
    available: { icon: "upgrade", text: "Update available" },
    prerelease: { icon: "science", text: "Pre-release" },
    unknown: { icon: "help", text: "Check unavailable" },
};
const STATUS_VIEW = UPDATE_STATE_VIEW;

const UNKNOWN_HINTS = {
    installed_unknown: "The installed version could not be determined",
    latest_unknown: "The latest upstream version could not be determined",
};

/** Map an /api/updates component entry to checking | current | available | unknown. */
export function resolveUpdateState(info) {
    if (!info || typeof info !== "object") return "unknown";
    if (info.status === "available" || info.status === "current" || info.status === "unknown") {
        return info.status;
    }
    // Older payloads carry no status field.
    if (info.update_available && info.latest) return "available";
    return info.latest ? "current" : "unknown";
}

/** Show *state* ("checking" | "current" | "available" | "unknown") in *cell*. */
export function renderUpdateState(cell, state, info = null) {
    const badge = cell.querySelector("[data-update-badge]");
    const status = cell.querySelector("[data-update-status]");

    if (state === "available" && info?.latest && badge) {
        // Informational only — updating means rebuilding the container image.
        const hint = `Version ${info.latest} is available — rebuild the image to update`;
        badge.href = info.url || "#";
        badge.title = hint;
        badge.setAttribute("aria-label", `${info.label || "Component"}: ${hint}`);
        badge.hidden = false;
        if (status) status.hidden = true;
        return;
    }

    if (badge) badge.hidden = true;
    if (!status) return;

    const view = STATUS_VIEW[state] || STATUS_VIEW.unknown;
    status.dataset.state = STATUS_VIEW[state] ? state : "unknown";
    const icon = status.querySelector("[data-update-status-icon]");
    const text = status.querySelector("[data-update-status-text]");
    if (icon) icon.textContent = view.icon;
    if (text) text.textContent = view.text;
    if (state === "current" && info?.latest) status.title = `Latest: ${info.latest}`;
    else if (state === "unknown") status.title = UNKNOWN_HINTS[info?.reason] || "";
    else status.title = "";
    status.hidden = false;
}

/** Render the final answer for *cell* from an /api/updates component entry. */
export function applyUpdateBadge(cell, info) {
    renderUpdateState(cell, resolveUpdateState(info), info);
}
