//
// app/static/js/current-job.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module current-job
 *
 * The card above the jobs list holding the job started in this page session -
 * so a fresh download stays visible when the mobile jobs list is collapsed.
 *
 * Holds exactly one job (starting another releases the previous back into the
 * list). After a reload the server hands over its active jobs and the newest
 * one is restored here; nothing is kept in browser storage. Mobile/tablet only - the desktop
 * table already shows a new job as its first row.
 */

import { buildJobCard, getJobById, isMobileJobsView, patchJobCard, prependJob, setDetachedJobId } from "./jobs.js";

let currentJobId = null;
let currentNode = null;

function getCard() {
    return document.getElementById("currentJobCard");
}

/**
 * Detach the current job from the list (or hand it back), depending on whether
 * the card is showing it. Re-renders both list surfaces in one pass.
 */
function syncDetachment() {
    setDetachedJobId(currentJobId && isMobileJobsView() ? currentJobId : "");
}

/**
 * Mount `job` on the card, releasing whichever job held it before.
 * Call this before storing the job, so the list renderers never mount a
 * second copy of it.
 * @param {object | null | undefined} job
 */
export function setCurrentJob(job) {
    const card = getCard();
    const jobId = String(job?.id ?? "");
    if (!card || !jobId) {
        return;
    }

    currentJobId = jobId;
    syncDetachment();

    // Built even while hidden on desktop, so a resize to mobile shows it at once.
    currentNode = buildJobCard(job);
    card.replaceChildren(currentNode);
    card.classList.remove("d-none");
}

/**
 * Rebuild the card from the active jobs the server rendered into the page.
 * Older active jobs stay in the list; one that history skipped is added to it.
 * @param {unknown} jobs - newest first
 */
export function restoreCurrentJob(jobs) {
    if (!Array.isArray(jobs)) {
        return;
    }

    const active = jobs.filter((job) => job && typeof job === "object" && job.id != null);
    // Oldest first, so the newest ends up at the top of the list.
    for (const job of [...active].reverse()) {
        if (!getJobById(String(job.id))) {
            prependJob(job);
        }
    }
    if (active.length > 0) {
        setCurrentJob(getJobById(String(active[0].id)) || active[0]);
    }
}

function readActiveJobs() {
    const node = document.getElementById("activeJobsBootstrapData");
    try {
        return JSON.parse(node?.textContent || "[]");
    } catch {
        return [];
    }
}

// jobs.js seeds its store on DOM ready; restore only after that.
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => restoreCurrentJob(readActiveJobs()));
} else {
    restoreCurrentJob(readActiveJobs());
}

// Crossing the breakpoint moves the job between the card and the list.
window.addEventListener("jobs-layout-change", syncDetachment);

/**
 * Patch the card when a live update arrives for the job it holds.
 * @param {object | null | undefined} job - a job or a partial update payload
 * @returns {boolean} whether the update belonged to the current job
 */
export function refreshCurrentJob(job) {
    const jobId = String(job?.id ?? "");
    if (!jobId || jobId !== currentJobId || !(currentNode instanceof HTMLElement)) {
        return false;
    }

    // Patch from the store (merged job), not the possibly-partial payload.
    patchJobCard(currentNode, getJobById(jobId) || job);
    return true;
}
