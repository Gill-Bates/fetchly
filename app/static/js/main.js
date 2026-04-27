//
// app/static/js/main.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";
import * as api from "./api.js";
import { getCookie, isValidYouTubeUrl, extractYouTubeVideoId, formatDuration } from "./utils.js";
import { prependJob, loadMore } from "./jobs.js";
import { connectWS } from "./ws.js";

const { fetchJobs, submitJob, fetchVideoInfo } = api;
const toErrorMessage = typeof api.toErrorMessage === "function"
    ? api.toErrorMessage
    : (value) => {
        if (value == null) return "";
        if (typeof value === "string") return value;
        if (typeof value?.detail === "string") return value.detail;
        if (typeof value?.error === "string") return value.error;
        return String(value);
    };

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
const detailModalEl = document.getElementById("detailModal");
const logoutBtn = document.getElementById("logoutBtn");
const settingsBtn = document.getElementById("settingsBtn");

const detailModal = (typeof bootstrap !== "undefined" && detailModalEl)
    ? bootstrap.Modal.getOrCreateInstance(detailModalEl)
    : null;
const defaultQualityHtml = qualitySelect ? qualitySelect.innerHTML : "";

let activeDetailId = null;
let lastFocusedBeforeModal = null;
let ticking = false;
let previewDebounceId = null;
let previewRequestSeq = 0;
let previewAbortController = null;

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
    if (metaTitle) metaTitle.textContent = "–";
    if (metaChannel) metaChannel.textContent = "–";
    if (metaUploader) metaUploader.textContent = "–";
    if (metaDuration) metaDuration.textContent = "–";
    if (metaViews) metaViews.textContent = "–";
    if (metaFormats) metaFormats.textContent = "–";
}

function resetQualityOptions() {
    if (qualitySelect) {
        qualitySelect.innerHTML = defaultQualityHtml;
    }
}

function hideVideoPreview() {
    if (videoPreviewGrid) {
        videoPreviewGrid.classList.add("d-none");
    }
    if (thumbnailPreview) {
        thumbnailPreview.replaceChildren();
    }
    resetVideoMeta();
    resetQualityOptions();
}

function showVideoPreview() {
    videoPreviewGrid?.classList.remove("d-none");
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
    icon.className = "material-symbols-outlined icon-inline";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "download";
    btnText.appendChild(icon);
    btnText.appendChild(document.createTextNode("Start"));
}

function updateQualityOptions(formats) {
    if (!qualitySelect) return;

    // For audio, keep default quality options (quality is ignored for audio anyway)
    if (typeSelect?.value === "audio") {
        resetQualityOptions();
        return;
    }

    qualitySelect.replaceChildren();
    if (!Array.isArray(formats) || formats.length === 0) {
        resetQualityOptions();
        return;
    }

    // Map YouTube format notes to our quality values
    const qualityMap = [
        { label: "Max (best available)", value: "max" },
        { label: "720p", value: "medium" },
        { label: "480p", value: "small" },
    ];

    const fragment = document.createDocumentFragment();
    for (const q of qualityMap) {
        const option = document.createElement("option");
        option.value = q.value;
        option.textContent = q.label;
        fragment.appendChild(option);
    }
    qualitySelect.appendChild(fragment);
}

async function updateVideoPreview() {
    previewAbortController?.abort();
    previewAbortController = new AbortController();

    const requestSeq = ++previewRequestSeq;
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
    if (metaTitle) metaTitle.textContent = "Loading...";
    if (metaFormats) metaFormats.textContent = "Loading...";

    try {
        const info = await fetchVideoInfo(url, { signal: previewAbortController.signal });
        if (requestSeq !== previewRequestSeq) {
            return;
        }
        if (metaTitle) metaTitle.textContent = info.title || "–";
        if (metaChannel) metaChannel.textContent = info.channel || "–";
        if (metaUploader) metaUploader.textContent = info.uploader || "–";
        if (metaDuration) metaDuration.textContent = formatDuration(info.duration);
        if (metaViews) metaViews.textContent = info.view_count?.toLocaleString() || "–";
        if (metaFormats) metaFormats.textContent = (info.formats || []).slice(0, 5).join(", ") || "–";
        updateQualityOptions(info.formats || []);
    } catch (error) {
        if (error?.name === "AbortError") {
            return;
        }
        if (requestSeq !== previewRequestSeq) {
            return;
        }
        if (metaTitle) metaTitle.textContent = "Metadata unavailable";
        if (metaFormats) metaFormats.textContent = "–";
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
    if (!detailModalEl || !detailModal) return;

    const row = document.querySelector(`tr[data-job-id="${CSS.escape(jobId)}"]`);
    if (!row) return;

    const cells = row.querySelectorAll("td");
    const statusValue = row.dataset.status || cells[5]?.querySelector(".status-pill")?.textContent?.trim() || "–";

    activeDetailId = jobId;
    document.getElementById("mId").textContent = jobId;
    document.getElementById("mUrl").textContent = row.dataset.url || cells[0]?.textContent || "";
    document.getElementById("mType").textContent = cells[1]?.textContent || "";
    document.getElementById("mQuality").textContent = cells[2]?.textContent || "";
    document.getElementById("mStatus").textContent = statusValue || "–";
    document.getElementById("mMessage").textContent = "–";

    const downloadBtn = document.getElementById("mDownloadBtn");
    if (downloadBtn) {
        downloadBtn.href = `/download/${encodeURIComponent(jobId)}`;
        downloadBtn.classList.toggle("d-none", statusValue !== "done");
    }

    detailModal.show();
}

document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action='open-detail']");
    if (!button) return;

    event.preventDefault();
    openDetail(button.dataset.jobId || "");
});

document.addEventListener("tubeyou:job-update", (event) => {
    const payload = event.detail;
    if (!payload || payload.id !== activeDetailId) return;

    document.getElementById("mStatus").textContent = payload.status || "–";
    document.getElementById("mMessage").textContent = payload.message || "–";
    const downloadBtn = document.getElementById("mDownloadBtn");
    if (downloadBtn) {
        downloadBtn.classList.toggle("d-none", payload.status !== "done");
    }
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
        if (lastFocusedBeforeModal && document.body.contains(lastFocusedBeforeModal)) {
            lastFocusedBeforeModal.focus();
            return;
        }

        urlInput?.focus();
    });
}

submitForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("");

    const urlValue = urlInput?.value.trim() || "";
    if (!isValidYouTubeUrl(urlValue)) {
        setError("Invalid YouTube URL. Please enter a valid youtube.com or youtu.be link.");
        return;
    }

    setSubmitBusy(true);

    try {
        const formData = new FormData(submitForm);
        // Audio downloads ignore quality selection - always use max (best audio)
        if (typeSelect?.value === "audio") {
            formData.set("quality", "max");
        }

        const job = await submitJob(formData, getCookie("tubeyou_csrf"));
        document.getElementById("emptyRow")?.remove();
        prependJob(job);
        submitForm.reset();
        typeSelect?.dispatchEvent(new Event("change"));
        hideVideoPreview();
        urlInput?.focus();
    } catch (error) {
        setError(`Error: ${error.message}`);
    } finally {
        setSubmitBusy(false);
    }
});

urlInput?.addEventListener("input", scheduleVideoPreviewUpdate);
urlInput?.addEventListener("paste", scheduleVideoPreviewUpdate);
urlInput?.addEventListener("change", scheduleVideoPreviewUpdate);

typeSelect?.addEventListener("change", () => {
    if (typeSelect.value === "audio") {
        // Set quality to "best" (first option) when audio format is selected.
        if (qualitySelect && qualitySelect.options.length > 0) {
            qualitySelect.selectedIndex = 0;
        }
        // Disable quality select for audio (always use best quality)
        if (qualitySelect) {
            qualitySelect.disabled = true;
        }
    } else {
        // Enable quality select for video format
        if (qualitySelect) {
            qualitySelect.disabled = false;
        }
    }
});

window.addEventListener("scroll", () => {
    if (ticking) return;

    ticking = true;
    window.requestAnimationFrame(() => {
        if (window.innerHeight + window.scrollY >= document.body.offsetHeight - CONFIG.SCROLL_OFFSET) {
            void loadMore(fetchJobs);
        }
        ticking = false;
    });
}, { passive: true });

logoutBtn?.addEventListener("click", async () => {
    try {
        await fetch("/logout", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": getCookie("tubeyou_csrf"),
            },
        });
    } catch (err) {
        console.warn("Logout request failed:", err);
    }

    window.location.assign("/login");
});

settingsBtn?.addEventListener("click", (event) => {
    // Fallback for environments/extensions that interfere with normal anchor navigation.
    event.preventDefault();
    window.location.assign("/settings");
});

// Security helpers
function isSafeRedirect(url) {
    if (typeof url !== "string") return false;
    try {
        const parsed = new URL(url, window.location.href);
        return parsed.origin === window.location.origin;
    } catch {
        return false;
    }
}

async function handleActionPost(btn, url, options = {}) {
    if (!btn || btn.dataset.loading === "1") {
        throw new Error("Action already in progress");
    }

    const originalDisabled = btn.disabled;
    btn.dataset.loading = "1";
    btn.disabled = true;

    const spinner = document.createElement("span");
    spinner.className = "spinner-border spinner-border-sm me-1";
    spinner.setAttribute("aria-hidden", "true");
    spinner.dataset.actionSpinner = "1";
    btn.prepend(spinner);

    try {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": getCookie("tubeyou_csrf"),
                ...options.headers,
            },
            body: options.body,
        });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(toErrorMessage(data.error || data.detail) || `HTTP ${response.status}`);
        }

        const successIcon = document.createElement("span");
        successIcon.className = "material-symbols-outlined icon-inline me-1";
        successIcon.setAttribute("aria-hidden", "true");
        successIcon.dataset.actionSuccess = "1";
        successIcon.textContent = "check";

        spinner.remove();
        btn.prepend(successIcon);
        setTimeout(() => {
            successIcon.remove();
            btn.disabled = originalDisabled;
            delete btn.dataset.loading;
        }, 2000);

        return data;
    } catch (err) {
        console.error("Action error:", err);
        setError(`Error: ${err.message}`);
        spinner.remove();
        btn.disabled = originalDisabled;
        delete btn.dataset.loading;
        throw err;
    }
}

// Lalal.ai split handler (delegated event listener)
document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action='lalal-split']");
    if (!btn) return;

    const jobId = btn.dataset.jobId;
    const stem = btn.dataset.stem;

    if (!jobId || !stem) return;

    // Listen for progress updates during this request
    let progressText = null;
    const progressHandler = (event) => {
        const detail = event.detail;
        if (detail.job_id === jobId && detail.stem === stem) {
            if (!progressText) {
                progressText = document.createElement("span");
                progressText.className = "ms-1 small";
                progressText.dataset.lalalProgress = "1";
                btn.appendChild(progressText);
            }
            const stage = detail.stage === "upload" ? "↑" : "⚙";
            progressText.textContent = `${stage} ${detail.progress}%`;
        }
    };
    document.addEventListener("tubeyou:lalal-progress", progressHandler);

    try {
        const data = await handleActionPost(
            btn,
            `/api/lalal/${encodeURIComponent(jobId)}?stem=${encodeURIComponent(stem)}`,
        );
        if (data.download_url && isSafeRedirect(data.download_url)) {
            window.location.href = data.download_url;
        }
    } catch {
        // Error already surfaced by handleActionPost.
    } finally {
        document.removeEventListener("tubeyou:lalal-progress", progressHandler);
        progressText?.remove();
    }
});

// Handle cancel job button clicks
document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action='cancel-job']");
    if (!btn) return;

    const jobId = btn.dataset.jobId;
    if (!jobId) return;

    if (!confirm("Are you sure you want to cancel this job?")) {
        return;
    }

    try {
        await handleActionPost(btn, `/api/jobs/${encodeURIComponent(jobId)}/cancel`);
    } catch {
        // Error already surfaced by handleActionPost.
    }
});

connectWS();