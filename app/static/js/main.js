//
// app/static/js/main.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { AUDIO_TYPE, CONFIG, DOWNLOADABLE_STATUSES, TERMINAL_STATUSES } from "./config.js";
import { fetchJobs, fetchResolvedThumbnail, fetchStats, submitJob, fetchVideoInfo, toErrorMessage } from "./api.js";
import { reportWarning } from "./errors.js";
import { createTimeoutSignal, getCsrfToken, humanSize, isValidMediaUrl, detectPlatform, platformPillLabel, PLATFORM, extractYouTubeVideoId, formatDuration, isSafeRedirect, subscribeToLalalProgress, triggerDownload } from "./utils.js";
import { prependJob, loadMore, applyJobUpdate, getJobById, applyStoredJobTitleFilter, formatCreatedText } from "./jobs.js?v=20260831a";
import { EVENT_NAMES, dispatchJobUpdate, setEventStreamEnabled } from "./events.js?v=20260831a";
import { showToast } from "./toast.js";
import { initTrim } from "./trim.js?v=20260831a";

const submitForm = document.getElementById("submitForm");
const urlInput = document.getElementById("urlInput");
const typeSelect = document.getElementById("typeSelect");
const qualitySelect = document.getElementById("qualitySelect");
const videoPreviewGrid = document.getElementById("videoPreviewGrid");
const thumbnailPreview = document.getElementById("thumbnailPreview");
const videoMeta = document.getElementById("videoMeta");
const metaTitle = document.getElementById("metaTitle");
const metaSummary = document.getElementById("metaSummary");
const metaFormats = document.getElementById("metaFormats");
const submitBtn = document.getElementById("submitBtn");
const btnText = document.getElementById("btnText");
const formError = document.getElementById("formError");
const jobsSearchInput = document.getElementById("jobsSearchInput");
const jobsSearchClear = document.getElementById("jobsSearchClear");
const jobsQuickFilter = document.getElementById("jobsQuickFilter");
const jobsTbody = document.getElementById("jobsTbody");
const jobsMobileList = document.getElementById("jobsMobileList");
const jobsRenderRoot = document.getElementById("jobsRenderRoot");
const jobsScrollContainer = document.querySelector("#jobsCard .jobs-list-shell");
const jobsSentinel = document.getElementById("jobsSentinel");
const detailModalEl = document.getElementById("detailModal");
const settingsBtn = document.getElementById("settingsBtn");
const titlePopover = document.getElementById("titlePopover");
const duplicateJobModalEl = document.getElementById("duplicateJobModal");
const duplicateJobMessage = document.getElementById("duplicateJobMessage");
const duplicateJobConfirmBtn = document.getElementById("duplicateJobConfirmBtn");
const DROPDOWN_TOGGLE_SELECTOR = "[data-bs-toggle='dropdown']";
const MOBILE_JOB_HISTORY_STORAGE_KEY = "fetchly.showJobHistory";
const MOBILE_JOB_HISTORY_CLASS = "mobile-job-history-enabled";

const detailModal = detailModalEl ? bootstrap.Modal.getOrCreateInstance(detailModalEl) : null;
const duplicateJobModal = duplicateJobModalEl ? bootstrap.Modal.getOrCreateInstance(duplicateJobModalEl) : null;
const statCards = new Map(
    [...document.querySelectorAll(".stat-card[data-stat-key]")].map((card) => [card.dataset.statKey, card]),
);
const defaultQualityOptions = qualitySelect
    ? [...qualitySelect.options].map((option) => ({
        value: option.value,
        text: option.textContent || "",
    }))
    : [];

let activeDetailId = null;
let lastFocusedBeforeModal = null;
let isLoadingMore = false;
let isSubmitting = false;
let pendingDuplicateFormData = null;
let previewDebounceId = null;
let previewAbortController = null;
let previewRequestUrl = "";
let filterRafId = null;
let activeJobStatusFilter = "all";
let activeTitleCell = null;
let statsRefreshTimer = null;
let detailInfoAbortController = null;
let dropdownObserver = null;
const inflightActions = new WeakSet();
const actionTimers = new WeakMap();

const STATS_REFRESHABLE_STATUSES = TERMINAL_STATUSES;
const COMPACT_NUMBER_FORMATTER = new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
});
const DETAIL_DATE_FORMATTER = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
});
const DETAIL_STATUS_LABELS = Object.freeze({
    analysis: "Processing",
    analysis_done: "Completed",
    cancelled: "Cancelled",
    done: "Completed",
    downloading: "Processing",
    error: "Failed",
    processing: "Processing",
    queued: "Queued",
    transcoding: "Processing",
});

function readMobileJobHistoryPreference() {
    try {
        return window.localStorage.getItem(MOBILE_JOB_HISTORY_STORAGE_KEY) === "true";
    } catch (_) {
        return false;
    }
}

function writeMobileJobHistoryPreference(enabled) {
    try {
        window.localStorage.setItem(MOBILE_JOB_HISTORY_STORAGE_KEY, String(enabled));
    } catch (_) {
        // Storage can be unavailable in private or restricted browser contexts.
    }
}

function syncMobileJobHistoryVisibility(enabled) {
    document.documentElement.classList.toggle(MOBILE_JOB_HISTORY_CLASS, enabled);

    const toggle = document.getElementById("showJobHistoryToggle");
    if (toggle instanceof HTMLInputElement) {
        toggle.checked = enabled;
    }
}

function initMobileJobHistoryToggle() {
    const toggle = document.getElementById("showJobHistoryToggle");
    if (!(toggle instanceof HTMLInputElement)) {
        return;
    }

    syncMobileJobHistoryVisibility(readMobileJobHistoryPreference());
    toggle.addEventListener("change", () => {
        const enabled = toggle.checked;
        writeMobileJobHistoryPreference(enabled);
        syncMobileJobHistoryVisibility(enabled);
    });
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
            return { value: String(Math.max(0, Math.trunc(Number(value) || 0))), unit: "" };
        case "total_minutes":
        case "total_lalal_minutes": {
            const rounded = Math.round(Math.max(0, Number(value) || 0) * 10) / 10;
            return { value: String(rounded), unit: "" };
        }
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
        reportWarning("Failed to refresh dashboard stats", {
            module: "main",
            action: "refreshDashboardStats",
            error: error?.message || String(error),
        });
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

function syncDashboardEventStream() {
    setEventStreamEnabled(true);
}

jobsRenderRoot?.addEventListener("mouseover", (event) => {
    const cell = event.target.closest(".job-title-popover-target");
    if (!(cell instanceof HTMLElement) || cell === activeTitleCell) return;
    showTitlePopover(cell);
});

jobsRenderRoot?.addEventListener("mouseout", (event) => {
    if (!activeTitleCell) return;

    const relatedCell = event.relatedTarget instanceof Element
        ? event.relatedTarget.closest(".job-title-popover-target")
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

document.addEventListener(EVENT_NAMES.JOB_UPDATE, (event) => {
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
    if (!jobsRenderRoot) {
        return;
    }

    dropdownObserver?.disconnect();

    initDropdowns(jobsRenderRoot);

    dropdownObserver = new MutationObserver((mutations) => {
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

    dropdownObserver.observe(jobsRenderRoot, { childList: true, subtree: true });
}

function cleanupDropdownObserver() {
    dropdownObserver?.disconnect();
    dropdownObserver = null;
}

function removeFilterEmptyState() {
    document.getElementById("jobsFilterEmptyRow")?.remove();
    document.getElementById("jobsFilterEmptyState")?.remove();
}

function getOrCreateFilterEmptyState() {
    if (window.matchMedia("(max-width: 1024px)").matches) {
        if (!jobsMobileList) return null;

        let state = document.getElementById("jobsFilterEmptyState");
        if (state) return state;

        state = document.createElement("div");
        state.id = "jobsFilterEmptyState";
        state.className = "jobs-mobile-empty empty-state empty-state--mobile d-none";

        const iconDiv = document.createElement("div");
        iconDiv.className = "material-symbols-outlined icon";
        iconDiv.setAttribute("aria-hidden", "true");
        iconDiv.textContent = "search";

        const p = document.createElement("p");
        p.textContent = "No jobs match this title or URL search.";

        state.append(iconDiv, p);
        jobsMobileList.append(state);
        return state;
    }

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
    iconDiv.className = "material-symbols-outlined icon";
    iconDiv.setAttribute("aria-hidden", "true");
    iconDiv.textContent = "search";

    const p = document.createElement("p");
    p.textContent = "No jobs match this title or URL search.";

    wrapper.append(iconDiv, p);

    td.appendChild(wrapper);
    row.appendChild(td);
    jobsTbody.appendChild(row);
    return row;
}

function getJobStatusFilterLabel(statusFilter) {
    return statusFilter === "done" ? "done" : statusFilter === "error" ? "error" : "job";
}

function getJobFilterEmptyMessage(query, statusFilter) {
    const hasQuery = query.trim() !== "";
    const hasStatusFilter = statusFilter !== "all";
    if (hasQuery && hasStatusFilter) {
        return "No jobs match this search and status filter.";
    }
    if (hasStatusFilter) {
        return `No ${getJobStatusFilterLabel(statusFilter)} jobs found.`;
    }
    return "No jobs match this title or URL search.";
}

function applyJobTitleFilter() {
    if (!jobsRenderRoot) return;

    const query = jobsSearchInput?.value || "";
    const { totalCount, visibleCount } = applyStoredJobTitleFilter(query, activeJobStatusFilter);
    const hasQuery = query.trim() !== "";
    const hasStatusFilter = activeJobStatusFilter !== "all";

    if (jobsSearchClear instanceof HTMLButtonElement) {
        jobsSearchClear.hidden = !hasQuery;
    }
    removeFilterEmptyState();
    const emptySearchRow = getOrCreateFilterEmptyState();
    if (emptySearchRow) {
        const hasRows = totalCount > 0;
        const message = emptySearchRow.querySelector("p");
        if (message) message.textContent = getJobFilterEmptyMessage(query, activeJobStatusFilter);
        emptySearchRow.classList.toggle("d-none", !((hasQuery || hasStatusFilter) && hasRows && visibleCount === 0));
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
        reportWarning("loadMore failed", {
            module: "main",
            action: "maybeLoadMoreJobs",
            error: err?.message || String(err),
        });
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
    setText(metaSummary, "–");
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
        qualitySelect.replaceChildren(
            ...defaultQualityOptions.map(({ value, text }) => {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = text;
                return option;
            }),
        );
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
    for (const q of defaultQualityOptions) {
        const option = document.createElement("option");
        option.value = q.value;
        option.textContent = q.text;
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

function getPreviewCreator(channel, uploader) {
    const primary = typeof channel === "string" ? channel.trim() : "";
    const secondary = typeof uploader === "string" ? uploader.trim() : "";
    if (!primary) {
        return secondary;
    }
    if (!secondary) {
        return primary;
    }
    return primary.localeCompare(secondary, undefined, { sensitivity: "accent" }) === 0
        ? primary
        : `${primary} / ${secondary}`;
}

function formatPreviewViewCount(viewCount) {
    const numeric = Number(viewCount);
    if (!Number.isFinite(numeric) || numeric <= 0) {
        return "";
    }
    return `${COMPACT_NUMBER_FORMATTER.format(numeric)} views`;
}

function renderPreviewSummary(info) {
    const parts = [
        getPreviewCreator(info?.channel, info?.uploader),
        formatDuration(info?.duration),
        formatPreviewViewCount(info?.view_count),
    ].filter(Boolean);
    setText(metaSummary, parts.join(" • ") || "Unknown source");
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
    hideVideoPreview();

    if (!isValidMediaUrl(url)) {
        previewRequestUrl = "";
        return;
    }

    const controller = new AbortController();
    previewAbortController = controller;

    // YouTube: show thumbnail immediately from stable img.youtube.com URL.
    // TikTok/Instagram: show placeholder until metadata resolves.
    const platform = detectPlatform(url);
    const videoId = platform === PLATFORM.YOUTUBE ? extractYouTubeVideoId(url) : "";
    if (videoId) {
        const image = document.createElement("img");
        image.src = `https://img.youtube.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
        image.alt = "Video Thumbnail";
        image.addEventListener("error", () => {
            thumbnailPreview?.replaceChildren();
        }, { once: true });
        thumbnailPreview?.replaceChildren(image);
    } else {
        thumbnailPreview?.replaceChildren();
    }

    showVideoPreview();
    setVideoMetaLoading(true);

    try {
        const info = await fetchVideoInfo(url, { signal: controller.signal });
        const currentUrl = urlInput?.value.trim() || "";
        if (controller.signal.aborted || currentUrl !== url) {
            return;
        }
        setText(metaTitle, info.title);
        renderPreviewSummary(info);
        renderPreviewFormats(info.formats, info.formats_total, info.formats_truncated);
        updateQualityOptions(info.formats || []);

        // For non-YouTube platforms, resolve and render a server-local cached thumbnail.
        if (platform !== PLATFORM.YOUTUBE) {
            let thumbnailSrc = "";
            try {
                const resolved = await fetchResolvedThumbnail(url, { signal: controller.signal });
                thumbnailSrc = typeof resolved.thumbnail_url === "string" ? resolved.thumbnail_url.trim() : "";
            } catch (resolveError) {
                reportWarning("thumbnail-resolve-failed", resolveError);
            }

            if (!thumbnailSrc && info.thumbnail) {
                thumbnailSrc = `/api/thumbnail-proxy?url=${encodeURIComponent(info.thumbnail)}`;
            }

            if (thumbnailSrc) {
                const image = document.createElement("img");
                image.src = thumbnailSrc;
                image.alt = "Video Thumbnail";
                image.addEventListener("error", () => {
                    thumbnailPreview?.replaceChildren();
                }, { once: true });
                thumbnailPreview?.replaceChildren(image);
            }
        }
    } catch (error) {
        if (controller.signal.aborted) {
            return;
        }
        // Clear stale metadata from the previous video before showing the error.
        resetVideoMeta();
        setText(metaTitle, "Metadata unavailable");
        setText(metaSummary, "Enter a valid public YouTube, TikTok, Instagram, or Facebook URL to load preview metadata.");
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

function abortDetailInfoRequest() {
    detailInfoAbortController?.abort();
    detailInfoAbortController = null;
}

function formatDetailStatus(status) {
    const normalized = String(status || "queued").trim().toLowerCase();
    if (DETAIL_STATUS_LABELS[normalized]) {
        return DETAIL_STATUS_LABELS[normalized];
    }

    const raw = normalized.replaceAll("_", " ");
    return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function formatDetailDate(value) {
    const text = value == null ? "" : String(value).trim();
    if (!text) return "";

    const sqliteUtc = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
    const date = new Date(sqliteUtc.test(text) ? `${text.replace(" ", "T")}Z` : text);
    return Number.isNaN(date.getTime()) ? "" : DETAIL_DATE_FORMATTER.format(date);
}

function formatDetailBitrate(value) {
    const bitrate = Number(value);
    return Number.isFinite(bitrate) && bitrate > 0 ? `${Math.round(bitrate)} kb/s` : "";
}

function formatDetailHost(value) {
    const text = value == null ? "" : String(value).trim();
    if (!text) return "";

    try {
        return new URL(text).hostname.replace(/^www\./i, "");
    } catch (_) {
        return "";
    }
}

function formatShortJobId(value) {
    const id = value == null ? "" : String(value).trim();
    return id.length > 16 ? `${id.slice(0, 8)}…${id.slice(-5)}` : id;
}

function setDetailSourceActions(url, jobId) {
    const sourceUrl = url == null ? "" : String(url).trim();
    const host = formatDetailHost(sourceUrl);
    setText(document.getElementById("mUrlHost"), host || "Source unavailable");

    const openUrl = document.getElementById("mOpenUrl");
    const canOpen = /^https:\/\//i.test(sourceUrl);
    if (openUrl instanceof HTMLAnchorElement) {
        openUrl.href = canOpen ? sourceUrl : "#";
        openUrl.classList.toggle("d-none", !canOpen);
    }

    const copyUrl = document.getElementById("mCopyUrl");
    if (copyUrl instanceof HTMLButtonElement) {
        copyUrl.dataset.copyValue = sourceUrl;
        copyUrl.disabled = !sourceUrl;
    }

    const copyId = document.getElementById("mCopyId");
    if (copyId instanceof HTMLButtonElement) {
        copyId.dataset.copyValue = String(jobId || "");
        copyId.disabled = !jobId;
    }
}

function formatDetailViewCount(viewCount) {
    const count = Number(viewCount);
    if (!Number.isFinite(count) || count <= 0) {
        return "";
    }

    return `${COMPACT_NUMBER_FORMATTER.format(count)} views`;
}

function setDetailChip(id, text) {
    const chip = document.getElementById(id);
    if (!(chip instanceof HTMLElement)) {
        return;
    }

    const value = typeof text === "string" ? text.trim() : "";
    const hasValue = Boolean(value) && value !== "–";
    chip.textContent = hasValue ? value : "";
    chip.classList.toggle("d-none", !hasValue);
}

function resetDetailFormats() {
    for (const sectionId of ["mAudioFormatsSection", "mVideoFormatsSection"]) {
        document.getElementById(sectionId)?.classList.add("d-none");
    }

    for (const containerId of ["mAudioFormats", "mVideoFormats"]) {
        const container = document.getElementById(containerId);
        if (container) {
            container.replaceChildren();
        }
    }
}

function resetDetailThumbnail() {
    const image = document.getElementById("mThumb");
    const fallback = document.getElementById("mThumbFallback");
    if (image instanceof HTMLImageElement) {
        image.src = "";
        image.classList.add("d-none");
    }
    fallback?.classList.remove("d-none");
}

function setDetailThumbnail(job) {
    const image = document.getElementById("mThumb");
    const fallback = document.getElementById("mThumbFallback");
    if (!(image instanceof HTMLImageElement)) {
        return;
    }

    const jobId = String(job?.id || "");
    if (!jobId) {
        resetDetailThumbnail();
        return;
    }

    // Use the server-cached thumbnail (downloaded by yt-dlp for every platform).
    // Falls back to the placeholder until the worker has fetched it.
    image.onload = () => {
        fallback?.classList.add("d-none");
        image.classList.remove("d-none");
    };
    image.onerror = () => {
        resetDetailThumbnail();
    };
    image.src = `/thumbnail/${encodeURIComponent(jobId)}`;
    image.alt = "Video thumbnail";
}

function createDetailFormatChip(format) {
    const chip = document.createElement("span");
    chip.className = "detail-chip detail-format-chip";

    const resolution = typeof format?.resolution === "string" ? format.resolution.trim() : "";
    const ext = typeof format?.ext === "string" ? format.ext.trim().toUpperCase() : "";
    chip.textContent = [ext, resolution].filter(Boolean).join(" · ");
    return chip;
}

function renderDetailFormatGroup(sectionId, containerId, formats) {
    const section = document.getElementById(sectionId);
    const container = document.getElementById(containerId);
    if (!(section instanceof HTMLElement) || !(container instanceof HTMLElement)) {
        return;
    }

    container.replaceChildren();
    if (!formats.length) {
        section.classList.add("d-none");
        return;
    }

    const fragment = document.createDocumentFragment();
    for (const format of formats.slice(0, 6)) {
        fragment.append(createDetailFormatChip(format));
    }
    container.append(fragment);
    section.classList.remove("d-none");
}

function renderDetailFormats(formats) {
    if (!Array.isArray(formats)) {
        resetDetailFormats();
        return;
    }

    const audioFormats = [];
    const videoFormats = [];

    for (const format of formats) {
        const resolution = typeof format?.resolution === "string" ? format.resolution.trim().toLowerCase() : "";
        if (!resolution) {
            continue;
        }

        if (resolution.endsWith("p")) {
            videoFormats.push(format);
        } else {
            audioFormats.push(format);
        }
    }

    renderDetailFormatGroup("mAudioFormatsSection", "mAudioFormats", audioFormats);
    renderDetailFormatGroup("mVideoFormatsSection", "mVideoFormats", videoFormats);
}

function setDetailMetaLine({ channel, uploader, duration, viewCount }) {
    const metaLine = document.getElementById("mMetaLine");
    if (!(metaLine instanceof HTMLElement)) {
        return;
    }

    const creator = channel || uploader || "Unknown source";
    const parts = [
        creator,
        duration ? formatDuration(duration) : "",
        formatDetailViewCount(viewCount),
    ].filter(Boolean);
    metaLine.textContent = parts.join(" • ") || "Unknown source";
}

function renderDetailTitle(job, text) {
    const titleEl = document.getElementById("mTitle");
    if (!(titleEl instanceof HTMLElement)) {
        return;
    }
    titleEl.replaceChildren();

    const platform = job?.platform || detectPlatform(job?.url || "");
    const label = platformPillLabel(platform);
    if (label) {
        const pill = document.createElement("span");
        pill.className = `platform-pill platform-pill--${platform}`;
        pill.title = label;
        pill.setAttribute("role", "img");
        pill.setAttribute("aria-label", label);

        const icon = document.createElement("span");
        icon.className = `platform-pill__icon platform-pill__icon--${platform}`;
        icon.setAttribute("aria-hidden", "true");
        pill.appendChild(icon);
        titleEl.appendChild(pill);
    }

    const titleText = document.createElement("span");
    titleText.className = "detail-title-text";
    titleText.textContent = text;
    titleEl.appendChild(titleText);
}

function updateOpenDetailStatus(job) {
    const normalizedStatus = String(job?.status || "queued").trim().toLowerCase();
    const statusEl = document.getElementById("mStatus");
    setText(statusEl, formatDetailStatus(normalizedStatus));
    statusEl?.setAttribute("data-status", normalizedStatus);

    const statusDate = formatDetailDate(job?.finished_at || job?.created_at);
    const statusDateEl = document.getElementById("mStatusDate");
    setText(statusDateEl, statusDate);
    statusDateEl?.classList.toggle("d-none", !statusDate);

    const message = String(job.message || "").trim();
    const messageEl = document.getElementById("mMessage");
    if (messageEl instanceof HTMLElement) {
        const redundantMessage = /^(finished|completed|done)$/i.test(message);
        const showMessage = Boolean(message) && (!redundantMessage || normalizedStatus === "error");
        messageEl.textContent = showMessage ? message : "";
        messageEl.classList.toggle("d-none", !showMessage);
    }

    setDetailChip("mFileSize", humanSize(job.filesize_bytes));
    setDetailChip("mCodec", job.codec || "");
    setDetailChip("mBitrate", formatDetailBitrate(job.bitrate_kbps));
    setDetailChip("mBpm", job.bpm ? `${job.bpm} BPM` : "");

    const downloadBtn = document.getElementById("mDownloadBtn");
    if (downloadBtn instanceof HTMLAnchorElement) {
        downloadBtn.classList.toggle("d-none", !DOWNLOADABLE_STATUSES.has(job.status || ""));
    }
}

function populateDetailFromJob(job) {
    const jobId = getJobById(job.id)?.id || job.id || "";
    renderDetailTitle(job, job.video_title || job.url || "Untitled");

    setText(document.getElementById("mMetaLine"), job.url ? "Loading metadata…" : "Unknown source");
    setText(document.getElementById("mUrl"), job.url || "");
    const typeLabel = String(job.type || "");
    const qualityLabel = String(job.quality || "");
    setDetailChip("mType", typeLabel ? typeLabel.charAt(0).toUpperCase() + typeLabel.slice(1) : "");
    setDetailChip("mQuality", qualityLabel === "max" ? "Max" : qualityLabel);
    setDetailChip("mFileSize", humanSize(job.filesize_bytes));
    setDetailChip("mCodec", job.codec || "");
    setDetailChip("mBitrate", formatDetailBitrate(job.bitrate_kbps));
    setText(document.getElementById("mId"), formatShortJobId(jobId));
    document.getElementById("mId")?.setAttribute("title", jobId);
    setDetailSourceActions(job.url, jobId);
    resetDetailFormats();
    setDetailThumbnail(job);

    const downloadBtn = document.getElementById("mDownloadBtn");
    if (downloadBtn instanceof HTMLAnchorElement) {
        downloadBtn.href = `/download/${encodeURIComponent(job.id)}`;
    }
    updateOpenDetailStatus(job);
}

async function hydrateDetailMedia(job) {
    if (!job?.url) {
        return;
    }

    abortDetailInfoRequest();
    const controller = new AbortController();
    detailInfoAbortController = controller;

    try {
        const info = await fetchVideoInfo(job.url, { signal: controller.signal });
        if (controller.signal.aborted || activeDetailId !== String(job.id)) {
            return;
        }

        renderDetailTitle(job, info.title || job.video_title || job.url || "Untitled");
        setDetailMetaLine({
            channel: info.channel,
            uploader: info.uploader,
            duration: info.duration,
            viewCount: info.view_count,
        });
        renderDetailFormats(info.formats);
    } catch (error) {
        if (controller.signal.aborted) {
            return;
        }
        setDetailMetaLine({ channel: "Metadata unavailable" });
    } finally {
        if (detailInfoAbortController === controller) {
            detailInfoAbortController = null;
        }
    }
}

function openDetail(jobId) {
    if (!jobId || !detailModalEl || !detailModal) return;

    const job = getJobById(jobId);
    if (!job) return;

    activeDetailId = jobId;
    populateDetailFromJob(job);
    detailModal.show();
    void hydrateDetailMedia(job);
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
            { timeoutMs: 15 * 60_000 },
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
            const updatedJob = applyJobUpdate(payload);
            dispatchJobUpdate(updatedJob || payload);
        }
    } catch (err) {
        if (err?.name !== "AbortError") {
            showToast(`Cancel failed: ${err.message}`, "danger");
        }
    }
}

/**
 * Write text to the clipboard, falling back to a hidden textarea where the
 * async Clipboard API is unavailable (non-secure contexts, older browsers).
 * @param {string} value
 * @returns {Promise<void>} rejects when the copy could not be performed
 */
async function writeToClipboard(value) {
    if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return;
    }

    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    const copied = document.execCommand("copy");
    input.remove();
    if (!copied) throw new Error("Clipboard unavailable");
}

async function copyDetailValue(btn) {
    const value = String(btn?.dataset.copyValue || "");
    if (!value) return;

    try {
        await writeToClipboard(value);
        showToast("Copied to clipboard", "success", 1800);
    } catch (error) {
        showToast("Could not copy to clipboard", "warning", 2200);
    }
}

async function handleShareJob(btn) {
    const jobId = String(btn?.dataset.jobId || "");
    if (!jobId) return;

    let payload;
    try {
        payload = await handleActionPost(btn, `/api/share/${encodeURIComponent(jobId)}`);
    } catch (err) {
        if (err?.name !== "AbortError") {
            showToast(`Share failed: ${err.message}`, "danger");
        }
        return;
    }

    const url = String(payload?.url || "");
    if (!url) {
        showToast("No share link returned", "warning");
        return;
    }

    const maxUses = Number(payload?.max_uses ?? 0);
    const limitNote = maxUses > 0
        ? ` (valid for ${maxUses} download${maxUses === 1 ? "" : "s"})`
        : "";

    try {
        await writeToClipboard(url);
        showToast(`Share link copied${limitNote}`, "success", 2600);
    } catch (error) {
        // The link exists either way - show it so it is not lost to a
        // clipboard permission the browser refused.
        showToast(`Share link: ${url}`, "info", 8000);
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
    "copy-detail": (btn, event) => {
        event.preventDefault();
        void copyDetailValue(btn);
    },
    "share-job": (btn, event) => {
        event.preventDefault();
        void handleShareJob(btn);
    },
});

document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;

    const actionBtn = event.target.closest("[data-action]");
    if (!(actionBtn instanceof HTMLElement)) return;

    const handler = ACTION_HANDLERS[actionBtn.dataset.action || ""];
    handler?.(actionBtn, event);
});

document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (!(event.target instanceof Element)) return;

    const detailTarget = event.target.closest('.job-item__body[data-action="open-detail"]');
    if (!(detailTarget instanceof HTMLElement)) return;

    event.preventDefault();
    ACTION_HANDLERS["open-detail"]?.(detailTarget, event);
});

document.addEventListener(EVENT_NAMES.JOB_UPDATE, (event) => {
    const payload = event.detail;
    if (!payload) return;

    if (payload.id === activeDetailId) {
        const activeJob = getJobById(payload.id);
        if (activeJob) {
            updateOpenDetailStatus(activeJob);
        }
    }

    syncDashboardEventStream();
    scheduleJobTitleFilter();
});

window.addEventListener("jobs-layout-change", () => {
    hideTitlePopover();
    syncDashboardEventStream();
    scheduleJobTitleFilter();
});

jobsSearchInput?.addEventListener("input", scheduleJobTitleFilter);
jobsQuickFilter?.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest("[data-job-filter]");
    if (!(button instanceof HTMLButtonElement) || !jobsQuickFilter.contains(button)) return;

    const requestedFilter = button.dataset.jobFilter;
    if (!["all", "done", "error"].includes(requestedFilter)) return;

    activeJobStatusFilter = requestedFilter;
    jobsQuickFilter.dataset.activeFilter = requestedFilter;
    jobsQuickFilter.querySelectorAll("[data-job-filter]").forEach((filterButton) => {
        const isActive = filterButton === button;
        filterButton.classList.toggle("is-active", isActive);
        filterButton.setAttribute("aria-pressed", String(isActive));
    });
    scheduleJobTitleFilter();
});
jobsSearchClear?.addEventListener("click", () => {
    if (!(jobsSearchInput instanceof HTMLInputElement)) return;
    jobsSearchInput.value = "";
    scheduleJobTitleFilter();
    jobsSearchInput.focus();
});

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
        abortDetailInfoRequest();
        if (lastFocusedBeforeModal && document.body.contains(lastFocusedBeforeModal)) {
            lastFocusedBeforeModal.focus();
            return;
        }

        urlInput?.focus();
    });
}

function isDuplicateJobError(error) {
    return error?.status === 409 && error?.body?.detail === "duplicate_job";
}

function describeDuplicateJob(existingJob) {
    const title = existingJob?.video_title ? `"${existingJob.video_title}"` : "This video";
    if (existingJob?.status && TERMINAL_STATUSES.has(existingJob.status)) {
        const when = formatCreatedText(existingJob.created_at);
        return when
            ? `${title} was already downloaded on ${when}. Do you want to download it again?`
            : `${title} was already downloaded. Do you want to download it again?`;
    }
    return `${title} is already being processed. Do you want to start it again anyway?`;
}

function onSubmitSuccess(job) {
    prependJob(job);
    syncDashboardEventStream();
    scheduleJobTitleFilter();
    showToast("Download job started", "success", 2500);

    submitForm.reset();
    typeSelect?.dispatchEvent(new Event("change"));
    hideVideoPreview();
    urlInput?.focus();
}

submitForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (isSubmitting) return;
    isSubmitting = true;

    setError("");

    try {
        const urlValue = urlInput?.value.trim() || "";
        if (!isValidMediaUrl(urlValue)) {
            setError("Unsupported URL. Please enter a valid YouTube, TikTok, Instagram, or Facebook link.");
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
        onSubmitSuccess(job);
    } catch (error) {
        if (isDuplicateJobError(error) && duplicateJobModal) {
            pendingDuplicateFormData = new FormData(submitForm);
            if (typeSelect?.value === AUDIO_TYPE) {
                pendingDuplicateFormData.set("quality", "max");
            }
            if (duplicateJobMessage) {
                duplicateJobMessage.textContent = describeDuplicateJob(error.body.existing_job);
            }
            duplicateJobModal.show();
            return;
        }
        setError(`Error: ${error.message}`);
    } finally {
        isSubmitting = false;
        setSubmitBusy(false);
    }
});

duplicateJobConfirmBtn?.addEventListener("click", async () => {
    if (!pendingDuplicateFormData || isSubmitting) return;
    isSubmitting = true;
    duplicateJobConfirmBtn.disabled = true;

    try {
        pendingDuplicateFormData.set("confirm_duplicate", "true");
        const job = await submitJob(pendingDuplicateFormData, getCsrfToken());
        duplicateJobModal?.hide();
        onSubmitSuccess(job);
    } catch (error) {
        duplicateJobModal?.hide();
        setError(`Error: ${error.message}`);
    } finally {
        isSubmitting = false;
        duplicateJobConfirmBtn.disabled = false;
    }
});

duplicateJobModalEl?.addEventListener("hidden.bs.modal", () => {
    pendingDuplicateFormData = null;
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
    cleanupDropdownObserver();

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

window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
        observeJobDropdowns();
        syncDashboardEventStream();
        scheduleJobTitleFilter();
    }
});

window.addEventListener("beforeunload", cleanupDropdownObserver);

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
 * @param {{ timeoutMs?: number, headers?: Record<string, string>, body?: BodyInit }} [options]
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
    const {
        timeoutMs = 30_000,
        headers = {},
        body,
    } = options;
    const { signal, cleanup: cleanupTimeoutSignal } = createTimeoutSignal(timeoutMs);

    try {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": getCsrfToken(),
                ...headers,
            },
            body,
            signal,
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
        cleanupTimeoutSignal();
        inflightActions.delete(btn);
    }
}

initMobileJobHistoryToggle();
syncDashboardEventStream();
applyJobTitleFilter();
setupInfiniteJobsScroll();
observeJobDropdowns();
initTrim();
