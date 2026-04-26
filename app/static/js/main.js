//
// app/static/js/main.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";
import { fetchJobs, submitJob, fetchVideoInfo } from "./api.js";
import { getCookie, isValidYouTubeUrl, extractYouTubeVideoId, formatDuration } from "./utils.js";
import { prependJob, loadMore } from "./jobs.js";
import { connectWS } from "./ws.js";

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

const detailModal = detailModalEl ? bootstrap.Modal.getOrCreateInstance(detailModalEl) : null;
const defaultQualityHtml = qualitySelect ? qualitySelect.innerHTML : "";

let activeDetailId = null;
let lastFocusedBeforeModal = null;
let ticking = false;
let previewDebounceId = null;
let previewRequestSeq = 0;

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

    qualitySelect.replaceChildren();
    if (!Array.isArray(formats) || formats.length === 0) {
        resetQualityOptions();
        return;
    }

    for (const format of formats) {
        const option = document.createElement("option");
        option.value = format.toLowerCase().replace(/[^\w]/g, "");
        option.textContent = format;
        qualitySelect.appendChild(option);
    }

    if (typeSelect?.value === "audio") {
        qualitySelect.selectedIndex = 0;
    }
}

async function updateVideoPreview() {
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
    image.src = `https://img.youtube.com/vi/${videoId}/hqdefault.jpg`;
    image.alt = "Video Thumbnail";
    image.addEventListener("error", () => {
        hideVideoPreview();
    });

    thumbnailPreview?.replaceChildren(image);
    showVideoPreview();
    if (metaTitle) metaTitle.textContent = "Loading...";
    if (metaFormats) metaFormats.textContent = "Loading...";

    try {
        const info = await fetchVideoInfo(url);
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
    } catch {
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

    const row = document.querySelector(`tr[data-job-id="${jobId}"]`);
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
        downloadBtn.href = `/download/${jobId}`;
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
        const job = await submitJob(new FormData(submitForm), getCookie("tubeyou_csrf"));
        document.getElementById("emptyRow")?.remove();
        prependJob(job);
        submitForm.reset();
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
urlInput?.addEventListener("change", () => {
    void updateVideoPreview();
});
urlInput?.addEventListener("blur", () => {
    void updateVideoPreview();
});

typeSelect?.addEventListener("change", () => {
    if (typeSelect.value === "audio" && qualitySelect && qualitySelect.options.length > 0) {
        qualitySelect.selectedIndex = 0;
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
    } catch {
        // Continue to the login page even if logout fails.
    }

    window.location.assign("/login");
});

connectWS();