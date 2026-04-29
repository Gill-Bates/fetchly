//
// app/static/js/main.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG, DOWNLOADABLE_STATUSES } from "./config.js";
import { fetchJobs, submitJob, fetchVideoInfo, toErrorMessage } from "./api.js";
import { getCookie, isValidYouTubeUrl, extractYouTubeVideoId, formatDuration, isSafeRedirect, subscribeToLalalProgress } from "./utils.js";
import { prependJob, loadMore, applyRowStatusClasses } from "./jobs.js?v=20260429l";
import { connectWS } from "./ws.js?v=20260429m";
import { showToast, clearToasts, toast } from "./toast.js";
import { initTrim } from "./trim.js?v=20260429v";

// Expose toast globally for inline scripts
window.TubeYou = Object.freeze({ toast, showToast });

const submitForm = document.getElementById("submitForm");
const urlInput = document.getElementById("urlInput");
const typeSelect = document.getElementById("typeSelect");
const qualitySelect = document.getElementById("qualitySelect");
const videoPreviewGrid = document.getElementById("videoPreviewGrid");
const thumbnailPreview = document.getElementById("thumbnailPreview");
const metaTitle = document.getElementById("metaTitle");
const metaChannel = document.getElementById("metaChannel");
const metaUploader = document.getElementById("metaUploader");
const metaDuration = document.getElementById("metaDuration");
const metaViews = document.getElementById("metaViews");
const metaFormats = document.getElementById("metaFormats");
const submitBtn = document.getElementById("submitBtn");
const btnText = document.getElementById("btnText");
const formError = document.getElementById("formError");
const jobsSearchInput = document.getElementById("jobsSearchInput");
const jobsTbody = document.getElementById("jobsTbody");
const jobsScrollContainer = document.querySelector("#jobsCard .table-responsive");
const jobsSentinel = document.getElementById("jobsSentinel");
const detailModalEl = document.getElementById("detailModal");
const settingsBtn = document.getElementById("settingsBtn");
const titlePopover = document.getElementById("titlePopover");

const detailModal = (typeof bootstrap !== "undefined" && detailModalEl)
    ? bootstrap.Modal.getOrCreateInstance(detailModalEl)
    : null;
const defaultQualityHtml = qualitySelect ? qualitySelect.innerHTML : "";
const AUDIO_TYPE = "audio";
const VIDEO_QUALITY_OPTIONS = [
    { label: "Max (best available)", value: "max" },
    { label: "720p", value: "medium" },
    { label: "480p", value: "small" },
];

let activeDetailId = null;
let lastFocusedBeforeModal = null;
let isLoadingMore = false;
let isSubmitting = false;
let previewDebounceId = null;
let previewAbortController = null;
let filterRafId = null;
let activeTitleCell = null;
const inflightActions = new WeakSet();
const actionTimers = new WeakMap();

function getCsrfToken() {
    return getCookie("tubeyou_csrf");
}

function isNativeNavigation(event) {
    return event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0;
}

function positionTitlePopover(target) {
    if (!titlePopover || !target?.isConnected) return;

    const rect = target.getBoundingClientRect();
    const margin = 8;
    const viewportPadding = 12;
    const measuredWidth = titlePopover.offsetWidth || 420;
    const measuredHeight = titlePopover.offsetHeight || 60;

    let left = rect.left;
    if (left + measuredWidth > window.innerWidth - viewportPadding) {
        left = window.innerWidth - measuredWidth - viewportPadding;
    }
    left = Math.max(viewportPadding, left);

    const fitsBelow = rect.bottom + margin + measuredHeight <= window.innerHeight - viewportPadding;
    titlePopover.dataset.placement = fitsBelow ? "bottom" : "top";
    titlePopover.style.left = `${left}px`;
    titlePopover.style.top = fitsBelow
        ? `${rect.bottom + margin}px`
        : `${Math.max(viewportPadding, rect.top - margin)}px`;
}

function showTitlePopover(target) {
    if (!titlePopover) return;

    const text = target.dataset.popoverText || "";
    if (!text.trim()) return;

    activeTitleCell = target;
    titlePopover.textContent = text;
    positionTitlePopover(target);
    titlePopover.classList.add("visible");
}

function hideTitlePopover() {
    if (!titlePopover) return;
    titlePopover.classList.remove("visible");
    titlePopover.textContent = "";
    delete titlePopover.dataset.placement;
    activeTitleCell = null;
}

/**
 * Set element text content safely. Falls back to "–" when value is null/undefined.
 * @param {Element | null | undefined} el
 * @param {string | null | undefined} text
 */
function setText(el, text) {
    if (el) el.textContent = text ?? "–";
}

document.addEventListener("mouseover", (event) => {
    const cell = event.target.closest(".job-title-cell");
    if (!(cell instanceof HTMLElement) || cell === activeTitleCell) return;
    showTitlePopover(cell);
});

document.addEventListener("mouseout", (event) => {
    if (!activeTitleCell) return;

    const relatedCell = event.relatedTarget instanceof Element
        ? event.relatedTarget.closest(".job-title-cell")
        : null;

    if (relatedCell === activeTitleCell) return;
    hideTitlePopover();
});

window.addEventListener("scroll", () => {
    if (!activeTitleCell) return;
    if (!activeTitleCell.isConnected) {
        hideTitlePopover();
        return;
    }
    positionTitlePopover(activeTitleCell);
}, true);

window.addEventListener("resize", () => {
    if (activeTitleCell) positionTitlePopover(activeTitleCell);
});

document.addEventListener("click", (event) => {
    const toggle = event.target instanceof Element
        ? event.target.closest("[data-bs-toggle='dropdown']")
        : null;

    if (!(toggle instanceof HTMLElement) || !toggle.closest("#jobsCard")) {
        return;
    }

    // Initialize with fixed positioning if not already done
    if (typeof bootstrap !== "undefined" && bootstrap.Dropdown) {
        let dropdown = bootstrap.Dropdown.getInstance(toggle);
        if (!dropdown) {
            dropdown = new bootstrap.Dropdown(toggle, {
                boundary: "viewport",
                popperConfig(defaultBsConfig) {
                    return {
                        ...(defaultBsConfig || {}),
                        strategy: "fixed",
                    };
                },
            });
            // Manually toggle since we intercepted the click
            event.preventDefault();
            event.stopPropagation();
            dropdown.toggle();
        }
    }
}, true);

function getOrCreateFilterEmptyRow() {
    if (!jobsTbody) return null;

    let row = document.getElementById("jobsFilterEmptyRow");
    if (row) return row;

    row = document.createElement("tr");
    row.id = "jobsFilterEmptyRow";
    row.classList.add("d-none");

    const td = document.createElement("td");
    td.colSpan = 8;

    const wrapper = document.createElement("div");
    wrapper.className = "empty-state";

    const iconDiv = document.createElement("div");
    iconDiv.className = "icon";
    iconDiv.textContent = "🔎";

    const p = document.createElement("p");
    p.textContent = "No jobs match this title search.";

    wrapper.append(iconDiv, p);

    td.appendChild(wrapper);
    row.appendChild(td);
    jobsTbody.appendChild(row);
    return row;
}

function applyJobTitleFilter() {
    if (!jobsTbody) return;

    const query = (jobsSearchInput?.value || "").trim().toLowerCase();
    const rows = jobsTbody.querySelectorAll("tr[data-job-id]");
    let visibleCount = 0;
    const changes = [];

    // Single read pass: collect changes
    for (const row of rows) {
        const titleCell = row.querySelector("td[data-label='Title']");
        const text = (titleCell?.textContent || "").toLowerCase();
        const shouldHide = query && !text.includes(query);
        const isHidden = row.classList.contains("d-none");
        if (shouldHide !== isHidden) {
            changes.push({ row, shouldHide });
        }
        if (!shouldHide) visibleCount++;
    }

    // Single write pass: apply changes
    for (const { row, shouldHide } of changes) {
        row.classList.toggle("d-none", shouldHide);
    }

    const emptySearchRow = getOrCreateFilterEmptyRow();
    if (emptySearchRow) {
        const hasRows = rows.length > 0;
        emptySearchRow.classList.toggle("d-none", !(query && hasRows && visibleCount === 0));
    }
}

function scheduleJobTitleFilter() {
    if (filterRafId != null) {
        cancelAnimationFrame(filterRafId);
    }
    filterRafId = requestAnimationFrame(() => {
        filterRafId = null;
        applyJobTitleFilter();
    });
}

async function maybeLoadMoreJobs() {
    if (isLoadingMore) return;

    isLoadingMore = true;
    try {
        await loadMore(fetchJobs);
        scheduleJobTitleFilter();
    } catch (err) {
        console.warn("loadMore failed:", err);
    } finally {
        isLoadingMore = false;
    }
}

function setupInfiniteJobsScroll() {
    if (!jobsScrollContainer) return;

    if (typeof IntersectionObserver === "function" && jobsSentinel) {
        const observer = new IntersectionObserver(
            (entries) => {
                const entry = entries[0];
                if (!entry?.isIntersecting || isLoadingMore) return;
                void maybeLoadMoreJobs();
            },
            {
                root: jobsScrollContainer,
                rootMargin: `0px 0px ${CONFIG.SCROLL_OFFSET}px 0px`,
                threshold: 0.01,
            },
        );

        observer.observe(jobsSentinel);
        return;
    }

    const onScroll = () => {
        if (isLoadingMore) return;

        const nearBottom =
            jobsScrollContainer.scrollTop + jobsScrollContainer.clientHeight
            >= jobsScrollContainer.scrollHeight - CONFIG.SCROLL_OFFSET;

        if (!nearBottom) return;
        void maybeLoadMoreJobs();
    };

    jobsScrollContainer.addEventListener("scroll", onScroll, { passive: true });
}

function setError(message) {
    if (!formError) return;
    if (message) {
        formError.textContent = message;
        formError.classList.remove("d-none");
        return;
    }

    formError.textContent = "";
    formError.classList.add("d-none");
}

function resetVideoMeta() {
    setText(metaTitle, "–");
    setText(metaChannel, "–");
    setText(metaUploader, "–");
    setText(metaDuration, "–");
    setText(metaViews, "–");
    setText(metaFormats, "–");
}

function resetQualityOptions() {
    if (qualitySelect) {
        qualitySelect.innerHTML = defaultQualityHtml;
    }
}

function hideVideoPreview() {
    if (videoPreviewGrid) {
        videoPreviewGrid.classList.add("d-none", "is-empty");
    }
    if (thumbnailPreview) {
        thumbnailPreview.replaceChildren();
    }
    resetVideoMeta();
    resetQualityOptions();
}

function showVideoPreview() {
    videoPreviewGrid?.classList.remove("d-none", "is-empty");
}

function setSubmitBusy(isBusy) {
    if (!submitBtn || !btnText) return;

    submitBtn.disabled = isBusy;
    if (isBusy) {
        btnText.replaceChildren();
        const spinner = document.createElement("span");
        spinner.className = "spinner-border spinner-border-sm me-2";
        spinner.setAttribute("aria-hidden", "true");
        btnText.appendChild(spinner);
        btnText.appendChild(document.createTextNode("Processing..."));
        return;
    }

    btnText.replaceChildren();
    const icon = document.createElement("span");
    icon.className = "material-symbols-outlined";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "download";
    btnText.appendChild(icon);
    btnText.appendChild(document.createTextNode(" Start"));
}

function abortPreviewRequest() {
    previewAbortController?.abort();
    previewAbortController = null;
}

function abortAndResetPreview() {
    if (previewDebounceId) {
        clearTimeout(previewDebounceId);
        previewDebounceId = null;
    }
    abortPreviewRequest();
}

function updateQualityOptions(formats) {
    if (!qualitySelect) return;

    // For audio, keep default quality options (quality is ignored for audio anyway)
    if (typeSelect?.value === AUDIO_TYPE) {
        resetQualityOptions();
        return;
    }

    qualitySelect.replaceChildren();
    if (!Array.isArray(formats) || formats.length === 0) {
        resetQualityOptions();
        return;
    }

    const fragment = document.createDocumentFragment();
    for (const q of VIDEO_QUALITY_OPTIONS) {
        const option = document.createElement("option");
        option.value = q.value;
        option.textContent = q.label;
        fragment.appendChild(option);
    }
    qualitySelect.appendChild(fragment);
}

async function updateVideoPreview() {
    abortPreviewRequest();
    const controller = new AbortController();
    previewAbortController = controller;

    const url = urlInput?.value.trim() || "";
    hideVideoPreview();

    if (!isValidYouTubeUrl(url)) {
        return;
    }

    const videoId = extractYouTubeVideoId(url);
    if (!videoId) {
        return;
    }

    const image = document.createElement("img");
    image.src = `https://img.youtube.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
    image.alt = "Video Thumbnail";
    image.addEventListener("error", () => {
        hideVideoPreview();
    }, { once: true });

    thumbnailPreview?.replaceChildren(image);
    showVideoPreview();
    setText(metaTitle, "Loading...");
    setText(metaFormats, "Loading...");

    try {
        const info = await fetchVideoInfo(url, { signal: controller.signal });
        // Stale-check: was a new controller created after this request started?
        if (controller.signal.aborted) {
            return;
        }
        setText(metaTitle, info.title);
        setText(metaChannel, info.channel);
        setText(metaUploader, info.uploader);
        setText(metaDuration, formatDuration(info.duration));
        setText(metaViews, info.view_count?.toLocaleString());
        setText(metaFormats, (info.formats || []).slice(0, 5).join(", ") || "–");
        updateQualityOptions(info.formats || []);
    } catch (error) {
        if (controller.signal.aborted) {
            return;
        }
        // Clear stale metadata from the previous video before showing the error.
        resetVideoMeta();
        setText(metaTitle, "Metadata unavailable");
        setText(metaFormats, "–");
    } finally {
        if (previewAbortController === controller) {
            previewAbortController = null;
        }
    }
}

function scheduleVideoPreviewUpdate() {
    if (previewDebounceId) {
        clearTimeout(previewDebounceId);
    }

    previewDebounceId = setTimeout(() => {
        previewDebounceId = null;
        void updateVideoPreview();
    }, 400);
}

function openDetail(jobId) {
    if (!jobId || !detailModalEl || !detailModal) return;

    const row = document.querySelector(`tr[data-job-id="${CSS.escape(jobId)}"]`);
    if (!row) return;

    /** Read a cell's trimmed text by its data-label attribute. */
    const getCellText = (label) => {
        const cell = row.querySelector(`td[data-label='${CSS.escape(label)}']`);
        return cell?.textContent?.trim() || "–";
    };

    const statusValue = row.dataset.status ||
        row.querySelector("td[data-label='Status'] .status-pill, td[data-label='Status'] .status-inline")?.textContent?.trim() || "–";

    activeDetailId = jobId;
    setText(document.getElementById("mId"), jobId);
    setText(document.getElementById("mUrl"), row.dataset.url || "–");
    setText(document.getElementById("mType"), getCellText("Format"));
    setText(document.getElementById("mQuality"), getCellText("Quality"));
    setText(document.getElementById("mBpm"), row.dataset.bpm || getCellText("BPM"));
    setText(document.getElementById("mBpmConfidence"), row.dataset.bpmConfidence || "–");
    setText(document.getElementById("mStatus"), statusValue);
    setText(document.getElementById("mMessage"), row.dataset.message || "–");

    const downloadBtn = document.getElementById("mDownloadBtn");
    if (downloadBtn) {
        downloadBtn.href = `/download/${encodeURIComponent(jobId)}`;
        downloadBtn.classList.toggle("d-none", !DOWNLOADABLE_STATUSES.has(statusValue));
    }

    detailModal.show();
}

async function handleLalalSplit(btn) {
    const jobId = btn.dataset.jobId;
    const stem = btn.dataset.stem;

    if (!jobId || !stem) return;

    const ac = new AbortController();
    let progressText = null;
    subscribeToLalalProgress(jobId, stem, (stage, progress) => {
        if (!progressText) {
            progressText = document.createElement("span");
            progressText.className = "ms-1 small";
            progressText.dataset.lalalProgress = "1";
            btn.appendChild(progressText);
        }
        progressText.textContent = `${stage} ${progress}%`;
    }, ac.signal);

    try {
        const data = await handleActionPost(
            btn,
            `/api/lalal/${encodeURIComponent(jobId)}?stem=${encodeURIComponent(stem)}`,
        );
        if (data.download_url && isSafeRedirect(data.download_url)) {
            // Cleanup before navigation
            ac.abort();
            progressText?.remove();
            window.location.assign(data.download_url);
            return;
        }
    } catch (err) {
        if (err?.name !== "AbortError") {
            showToast(`Split failed: ${err.message}`, "danger");
        }
    } finally {
        ac.abort();  // Removes listener immediately, regardless of outcome
        progressText?.remove();
    }
}

async function handleCancelJob(btn) {
    const jobId = btn.dataset.jobId;
    if (!jobId) return;

    if (!confirm("Are you sure you want to cancel this job?")) {
        return;
    }

    try {
        await handleActionPost(btn, `/api/jobs/${encodeURIComponent(jobId)}/cancel`);
    } catch (err) {
        if (err?.name !== "AbortError") {
            showToast(`Cancel failed: ${err.message}`, "danger");
        }
    }
}

const ACTION_HANDLERS = Object.freeze({
    "open-detail": (btn, event) => {
        event.preventDefault();
        openDetail(btn.dataset.jobId || "");
    },
    "lalal-split": (btn, event) => {
        event.preventDefault();
        void handleLalalSplit(btn);
    },
    "cancel-job": (btn, event) => {
        event.preventDefault();
        void handleCancelJob(btn);
    },
});

document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;

    const actionBtn = event.target.closest("[data-action]");
    if (!(actionBtn instanceof HTMLElement)) return;

    const handler = ACTION_HANDLERS[actionBtn.dataset.action || ""];
    handler?.(actionBtn, event);
});

document.addEventListener("tubeyou:job-update", (event) => {
    const payload = event.detail;
    if (!payload) return;

    if (payload.id === activeDetailId) {
        // Use setText() for null-safe updates; direct textContent assignment would
        // throw a TypeError if the modal element is absent.
        setText(document.getElementById("mStatus"), payload.status);
        setText(document.getElementById("mMessage"), payload.message);
        if (payload.bpm != null) {
            setText(document.getElementById("mBpm"), String(payload.bpm));
        }
        if (payload.bpm_confidence != null) {
            setText(document.getElementById("mBpmConfidence"), String(payload.bpm_confidence));
        }
        const downloadBtn = document.getElementById("mDownloadBtn");
        if (downloadBtn) {
            downloadBtn.classList.toggle("d-none", !DOWNLOADABLE_STATUSES.has(payload.status || ""));
        }
    }

    // Update the main job row CSS classes when status changes
    const mainRow = document.querySelector(
        `tr[data-job-id="${CSS.escape(String(payload.id))}"]`
    );
    if (mainRow && payload.status) {
        applyRowStatusClasses(mainRow, payload.status);
    }

    // Keep the open detail row in sync with live analysis data.
    const detailRow = document.getElementById(`detail-${payload.id}`);
    if (detailRow) {
        const update = (field, value) => {
            const el = detailRow.querySelector(`[data-detail-field="${field}"] span`);
            if (el && value != null) el.textContent = String(value) || "–";
        };
        update("bpm", payload.bpm);
        update("bpm_confidence", payload.bpm_confidence);
    }

    scheduleJobTitleFilter();
});

jobsSearchInput?.addEventListener("input", scheduleJobTitleFilter);

if (detailModalEl) {
    detailModalEl.addEventListener("show.bs.modal", () => {
        lastFocusedBeforeModal = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    });

    detailModalEl.addEventListener("hide.bs.modal", () => {
        const focused = document.activeElement;
        if (focused instanceof HTMLElement && detailModalEl.contains(focused)) {
            focused.blur();
        }
    });

    detailModalEl.addEventListener("hidden.bs.modal", () => {
        activeDetailId = null;
        if (lastFocusedBeforeModal && document.body.contains(lastFocusedBeforeModal)) {
            lastFocusedBeforeModal.focus();
            return;
        }

        urlInput?.focus();
    });
}

submitForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (isSubmitting) return;
    isSubmitting = true;

    setError("");

    try {
        const urlValue = urlInput?.value.trim() || "";
        if (!isValidYouTubeUrl(urlValue)) {
            setError("Invalid YouTube URL. Please enter a valid youtube.com or youtu.be link.");
            return;
        }

        // Kill any pending preview work so it cannot race the submit.
        abortAndResetPreview();
        setSubmitBusy(true);

        const formData = new FormData(submitForm);
        // Audio downloads ignore quality selection - always use max (best audio)
        if (typeSelect?.value === AUDIO_TYPE) {
            formData.set("quality", "max");
        }

        const job = await submitJob(formData, getCsrfToken());
        document.getElementById("emptyRow")?.remove();
        prependJob(job);
        scheduleJobTitleFilter();

        submitForm.reset();
        typeSelect?.dispatchEvent(new Event("change"));
        hideVideoPreview();
        urlInput?.focus();
    } catch (error) {
        setError(`Error: ${error.message}`);
    } finally {
        isSubmitting = false;
        setSubmitBusy(false);
    }
});

// input alone is sufficient; paste fires before input anyway
urlInput?.addEventListener("input", scheduleVideoPreviewUpdate);
// change as fallback for edge case: autofill without input event (Safari)
urlInput?.addEventListener("change", scheduleVideoPreviewUpdate);

typeSelect?.addEventListener("change", () => {
    if (typeSelect.value === AUDIO_TYPE) {
        // Set quality to "best" (first option) when audio format is selected.
        if (qualitySelect && qualitySelect.options.length > 0) {
            qualitySelect.selectedIndex = 0;
        }
        // Disable quality select for audio (always use best quality)
        if (qualitySelect) {
            qualitySelect.disabled = true;
            qualitySelect.classList.add("is-disabled");
        }
    } else {
        // Enable quality select for video format
        if (qualitySelect) {
            qualitySelect.disabled = false;
            qualitySelect.classList.remove("is-disabled");
        }
    }
});

settingsBtn?.addEventListener("click", (event) => {
    if (isNativeNavigation(event)) {
        return;
    }

    event.preventDefault();
    window.location.assign("/settings");
});

window.addEventListener("jobs-load-error", (event) => {
    const detail = event?.detail;
    const message = detail instanceof Error ? detail.message : "Could not load more jobs.";
    showToast(`Jobs load failed: ${message}`, "warning", 3500);
});

/**
 * POST to an action endpoint with CSRF protection, inflight deduplication,
 * and inline button feedback.
 *
 * Shows a spinner while the request is active and a temporary success icon on
 * completion. Concurrent calls for the same button are rejected.
 *
 * @param {HTMLElement} btn
 * @param {string} url
 * @param {{ headers?: Record<string, string>, body?: BodyInit }} [options]
 * @returns {Promise<object>}
 */
async function handleActionPost(btn, url, options = {}) {
    if (!btn || inflightActions.has(btn)) {
        throw new Error("Action already in progress");
    }

    inflightActions.add(btn);  // Synchronous lock in same microtask
    const originalDisabled = btn.disabled;
    btn.disabled = true;

    const spinner = document.createElement("span");
    spinner.className = "spinner-border spinner-border-sm me-1";
    spinner.setAttribute("aria-hidden", "true");
    btn.prepend(spinner);

    const existingTimer = actionTimers.get(btn);
    if (existingTimer) {
        clearTimeout(existingTimer);
        actionTimers.delete(btn);
    }
    btn.querySelector("[data-action-post-success='1']")?.remove();

    try {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": getCsrfToken(),
                ...options.headers,
            },
            body: options.body,
            // Prevent the button from staying disabled forever if the server hangs.
            signal: AbortSignal.timeout(30_000),
        });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(toErrorMessage(data.error || data.detail) || `HTTP ${response.status}`);
        }

        const successIcon = document.createElement("span");
        successIcon.className = "material-symbols-outlined icon-inline me-1";
    successIcon.dataset.actionPostSuccess = "1";
        successIcon.setAttribute("aria-hidden", "true");
        successIcon.textContent = "check";

        spinner.remove();
        btn.prepend(successIcon);

        const timerId = setTimeout(() => {
            actionTimers.delete(btn);
            if (successIcon.isConnected) {
                successIcon.remove();
            }
            if (btn.isConnected) {
                btn.disabled = originalDisabled;
            }
        }, 2000);
        actionTimers.set(btn, timerId);

        return data;
    } catch (err) {
        spinner.remove();
        if (btn.isConnected) {
            btn.disabled = originalDisabled;
        }
        throw err;
    } finally {
        inflightActions.delete(btn);
    }
}

connectWS();
applyJobTitleFilter();
setupInfiniteJobsScroll();
initTrim();
