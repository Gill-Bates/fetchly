//
// app/static/js/main.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { AUDIO_TYPE, CONFIG, DOWNLOADABLE_STATUSES, TERMINAL_STATUSES, VIDEO_QUALITY_OPTIONS } from "./config.js";
import { fetchJobs, fetchStats, submitJob, fetchVideoInfo, toErrorMessage } from "./api.js";
import { getCookie, humanSize, isValidYouTubeUrl, extractYouTubeVideoId, formatDuration, isSafeRedirect, subscribeToLalalProgress } from "./utils.js";
import { prependJob, loadMore, applyRowStatusClasses } from "./jobs.js?v=20260429m";
import { setEventStreamEnabled } from "./events.js?v=20260429m";
import { showToast, toast } from "./toast.js";
import { initTrim } from "./trim.js?v=20260429v";

// Expose toast globally for inline scripts
window.TubeYou = Object.freeze({ toast, showToast });

const submitForm = document.getElementById("submitForm");
const urlInput = document.getElementById("urlInput");
const typeSelect = document.getElementById("typeSelect");
const qualitySelect = document.getElementById("qualitySelect");
const videoPreviewGrid = document.getElementById("videoPreviewGrid");
const thumbnailPreview = document.getElementById("thumbnailPreview");
const videoMeta = document.getElementById("videoMeta");
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
const DROPDOWN_TOGGLE_SELECTOR = "[data-bs-toggle='dropdown']";

const detailModal = detailModalEl ? bootstrap.Modal.getOrCreateInstance(detailModalEl) : null;
const statCards = new Map(
    [...document.querySelectorAll(".stat-card[data-stat-key]")].map((card) => [card.dataset.statKey, card]),
);
const defaultQualityHtml = qualitySelect ? qualitySelect.innerHTML : "";

let activeDetailId = null;
let lastFocusedBeforeModal = null;
let isLoadingMore = false;
let isSubmitting = false;
let previewDebounceId = null;
let previewAbortController = null;
let previewRequestUrl = "";
let filterRafId = null;
let activeTitleCell = null;
let statsRefreshTimer = null;
const inflightActions = new WeakSet();
const actionTimers = new WeakMap();

const STATS_REFRESHABLE_STATUSES = new Set(["done"]);

async function copyTextToClipboard(text) {
    if (!text) {
        throw new Error("No source URL available");
    }

    await navigator.clipboard.writeText(text);
}

function triggerDownload(url) {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
}

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

function renderStatParts(statKey, value) {
    switch (statKey) {
    case "total_bytes": {
        const rendered = humanSize(value);
        const match = /^(.+?)\s+([^\s]+)$/.exec(rendered);
        return match
            ? { value: match[1], unit: match[2] }
            : { value: rendered, unit: "" };
    }
    case "total_jobs":
    case "total_minutes":
    case "total_lalal_minutes":
        return { value: String(Math.max(0, Math.trunc(Number(value) || 0))), unit: "" };
    default:
        return { value: String(value ?? 0), unit: "" };
    }
}

function applyDashboardStats(stats) {
    if (!stats || typeof stats !== "object") {
        return;
    }

    for (const [statKey, card] of statCards.entries()) {
        const valueNode = card.querySelector("[data-stat-value]");
        const unitNode = card.querySelector("[data-stat-unit]");
        if (!(valueNode instanceof HTMLElement)) {
            continue;
        }

        const rendered = renderStatParts(statKey, stats[statKey]);
        valueNode.textContent = rendered.value;
        if (unitNode instanceof HTMLElement) {
            unitNode.textContent = rendered.unit;
            unitNode.hidden = !rendered.unit;
        }
    }
}

async function refreshDashboardStats() {
    if (!statCards.size) {
        return;
    }

    try {
        applyDashboardStats(await fetchStats());
    } catch (error) {
        console.warn("Failed to refresh dashboard stats:", error);
    }
}

function scheduleDashboardStatsRefresh() {
    if (!statCards.size) {
        return;
    }

    if (statsRefreshTimer) {
        clearTimeout(statsRefreshTimer);
    }

    statsRefreshTimer = window.setTimeout(() => {
        statsRefreshTimer = null;
        void refreshDashboardStats();
    }, 250);
}

function hasNonTerminalJobs() {
    if (!jobsTbody) {
        return false;
    }

    return [...jobsTbody.querySelectorAll("tr[data-job-id]")].some((row) => {
        const status = row instanceof HTMLElement ? row.dataset.status || "" : "";
        return status !== "" && !TERMINAL_STATUSES.has(status);
    });
}

function syncDashboardEventStream() {
    setEventStreamEnabled(hasNonTerminalJobs());
}

jobsTbody?.addEventListener("mouseover", (event) => {
    const cell = event.target.closest(".job-title-cell");
    if (!(cell instanceof HTMLElement) || cell === activeTitleCell) return;
    showTitlePopover(cell);
});

jobsTbody?.addEventListener("mouseout", (event) => {
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
}, { passive: true });

window.addEventListener("resize", () => {
    if (activeTitleCell) positionTitlePopover(activeTitleCell);
});

jobsTbody?.addEventListener("click", (event) => {
    const copyBtn = event.target instanceof Element
        ? event.target.closest(".job-copy-url-btn")
        : null;

    if (copyBtn instanceof HTMLButtonElement) {
        event.preventDefault();
        event.stopPropagation();
        void copyTextToClipboard(copyBtn.dataset.copyUrl || "")
            .then(() => {
                showToast("Source URL copied", "success", 2200);
            })
            .catch((error) => {
                showToast(`Copy failed: ${error.message}`, "warning", 3200);
            });
    }
});

document.addEventListener("tubeyou:job-update", (event) => {
    const status = typeof event.detail?.status === "string" ? event.detail.status : "";
    if (STATS_REFRESHABLE_STATUSES.has(status)) {
        scheduleDashboardStatsRefresh();
    }
});

function getDropdownOptions() {
    return {
        boundary: "viewport",
        popperConfig(defaultBsConfig) {
            return {
                ...(defaultBsConfig || {}),
                strategy: "fixed",
            };
        },
    };
}

function forEachDropdownToggle(root, callback) {
    if (!(root instanceof Element)) {
        return;
    }

    if (root.matches(DROPDOWN_TOGGLE_SELECTOR)) {
        callback(root);
    }
    root.querySelectorAll(DROPDOWN_TOGGLE_SELECTOR).forEach((toggle) => {
        if (toggle instanceof HTMLElement) {
            callback(toggle);
        }
    });
}

function initDropdowns(root) {
    forEachDropdownToggle(root, (toggle) => {
        bootstrap.Dropdown.getOrCreateInstance(toggle, getDropdownOptions());
    });
}

function disposeDropdowns(root) {
    forEachDropdownToggle(root, (toggle) => {
        bootstrap.Dropdown.getInstance(toggle)?.dispose();
    });
}

function observeJobDropdowns() {
    if (!jobsTbody) {
        return;
    }

    initDropdowns(jobsTbody);

    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            mutation.addedNodes.forEach((node) => {
                if (node instanceof Element) {
                    initDropdowns(node);
                }
            });
            mutation.removedNodes.forEach((node) => {
                if (node instanceof Element) {
                    disposeDropdowns(node);
                }
            });
        }
    });

    observer.observe(jobsTbody, { childList: true, subtree: true });
}

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
        const text = (titleCell?.querySelector(".job-title-text")?.textContent || "").toLowerCase();
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
        syncDashboardEventStream();
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
    setVideoMetaLoading(false);
    setText(metaTitle, "–");
    setText(metaChannel, "–");
    setText(metaUploader, "–");
    setText(metaDuration, "–");
    setText(metaViews, "–");
    setText(metaFormats, "–");
}

function setVideoMetaLoading(isLoading) {
    if (!videoMeta) {
        return;
    }

    videoMeta.classList.toggle("is-loading", isLoading);
    videoMeta.setAttribute("aria-busy", isLoading ? "true" : "false");
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

function isPreviewVisible() {
    return Boolean(videoPreviewGrid && !videoPreviewGrid.classList.contains("d-none"));
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
    previewRequestUrl = "";
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

function normalizePreviewFormatLabel(resolution) {
    const raw = typeof resolution === "string" ? resolution.trim() : "";
    if (!raw) {
        return "";
    }

    const kbpsMatch = /^(\d+(?:\.\d+)?)\s*kbps$/i.exec(raw);
    if (kbpsMatch) {
        return `${Math.round(Number(kbpsMatch[1]))} kbps`;
    }

    return raw;
}

function renderPreviewFormats(formats, totalFormats = 0, isTruncated = false) {
    if (!metaFormats) {
        return;
    }

    metaFormats.classList.add("meta-value--badges");
    metaFormats.replaceChildren();

    if (!Array.isArray(formats) || formats.length === 0) {
        metaFormats.textContent = "–";
        return;
    }

    const items = formats
        .slice(0, 5)
        .map((format) => {
            if (!format || typeof format !== "object") {
                return null;
            }

            const label = normalizePreviewFormatLabel(format.resolution);
            const ext = typeof format.ext === "string" ? format.ext.trim().toLowerCase() : "";
            if (!label && !ext) {
                return null;
            }

            return { label: label || "Format", ext };
        })
        .filter(Boolean);

    if (items.length === 0) {
        metaFormats.textContent = "–";
        return;
    }

    const fragment = document.createDocumentFragment();
    for (const item of items) {
        const badge = document.createElement("span");
        badge.className = "meta-badge";

        const label = document.createElement("span");
        label.className = "meta-badge__label";
        label.textContent = item.label;
        badge.appendChild(label);

        if (item.ext) {
            const ext = document.createElement("span");
            ext.className = "meta-badge__ext";
            ext.textContent = item.ext;
            badge.appendChild(ext);
        }

        fragment.appendChild(badge);
    }

    if (isTruncated && totalFormats > items.length) {
        const moreBadge = document.createElement("span");
        moreBadge.className = "meta-badge meta-badge--more";
        moreBadge.textContent = `+${totalFormats - items.length} more`;
        fragment.appendChild(moreBadge);
    }

    metaFormats.appendChild(fragment);
}

async function updateVideoPreview() {
    const url = urlInput?.value.trim() || "";
    previewRequestUrl = url;

    abortPreviewRequest();
    const controller = new AbortController();
    previewAbortController = controller;
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
    setVideoMetaLoading(true);

    try {
        const info = await fetchVideoInfo(url, { signal: controller.signal });
        const currentUrl = urlInput?.value.trim() || "";
        if (controller.signal.aborted || currentUrl !== url) {
            return;
        }
        setText(metaTitle, info.title);
        setText(metaChannel, info.channel);
        setText(metaUploader, info.uploader);
        setText(metaDuration, formatDuration(info.duration));
        setText(metaViews, info.view_count?.toLocaleString());
        renderPreviewFormats(info.formats, info.formats_total, info.formats_truncated);
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
            setVideoMetaLoading(false);
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

function scheduleVideoPreviewUpdateFromChange() {
    const nextUrl = urlInput?.value.trim() || "";
    const sameUrl = nextUrl !== "" && nextUrl === previewRequestUrl;
    if (sameUrl && (previewDebounceId != null || previewAbortController || isPreviewVisible())) {
        return;
    }

    scheduleVideoPreviewUpdate();
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

    const statusValue = row.dataset.status || "–";

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
        if (!data.download_url) {
            showToast("No download URL returned", "warning");
            return;
        }

        if (!isSafeRedirect(data.download_url)) {
            showToast("Download URL rejected as unsafe", "danger");
            return;
        }

        if (typeof data.download_url === "string") {
            triggerDownload(data.download_url);
            showToast("Download started", "success", 2200);
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
        const payload = await handleActionPost(btn, `/api/jobs/${encodeURIComponent(jobId)}/cancel`);
        if (payload && typeof payload === "object") {
            document.dispatchEvent(new CustomEvent("tubeyou:job-update", { detail: payload }));
        }
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

    syncDashboardEventStream();
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
        syncDashboardEventStream();
        scheduleJobTitleFilter();
        showToast("Download job started", "success", 2500);

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
urlInput?.addEventListener("change", scheduleVideoPreviewUpdateFromChange);

typeSelect?.addEventListener("change", () => {
    if (typeSelect.value === AUDIO_TYPE) {
        // Reset quality to the first option when audio is selected;
        // audio ignores the choice and the backend enforces max quality.
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

window.addEventListener("pagehide", () => {
    if (statsRefreshTimer) {
        clearTimeout(statsRefreshTimer);
        statsRefreshTimer = null;
    }

    if (filterRafId != null) {
        cancelAnimationFrame(filterRafId);
        filterRafId = null;
    }

    if (previewDebounceId) {
        clearTimeout(previewDebounceId);
        previewDebounceId = null;
    }

    abortPreviewRequest();
});

/**
 * POST to an action endpoint with CSRF protection, inflight deduplication,
 * and inline button feedback.
 *
 * Disables the button while the request is active and shows a spinner.
 * On success, briefly shows a checkmark icon before restoring the button state.
 * Throws if the same button already has an in-flight request.
 *
 * @param {HTMLElement} btn - Triggering button used as the inflight lock key.
 * @param {string} url - Endpoint URL for the POST request.
 * @param {{ headers?: Record<string, string>, body?: BodyInit }} [options]
 * @returns {Promise<object>} Parsed JSON response body.
 * @throws {Error} If the button is missing, already busy, or the request fails.
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
            throw new Error(toErrorMessage(data) || `HTTP ${response.status}`);
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

syncDashboardEventStream();
applyJobTitleFilter();
setupInfiniteJobsScroll();
observeJobDropdowns();
initTrim();
