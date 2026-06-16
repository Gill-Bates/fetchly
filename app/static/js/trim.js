//
// app/static/js/trim.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * trim.js - Audio trim workflow with waveform visualization
 * 
 * Provides a modal-based UI for trimming audio files before Lalal.ai processing.
 * Uses wavesurfer.js for waveform rendering and region selection.
 */

import { showToast } from "./toast.js";
import { reportError } from "./errors.js";
import { getJobById } from "./jobs.js";
import { CSRF_COOKIE_NAME, LALAL_MAX_DURATION_SECONDS } from "./config.js";
import {
    buildTrimId,
    clamp,
    getCookie,
    isSafeRedirect,
    normalizeTimeRange,
    subscribeToLalalProgress,
    triggerDownload,
    SNAP_INTERVAL_SECONDS,
} from "./utils.js";
import { isLalalEnabled, isLalalDurationGuardEnabled, isDurationBlocked as isJobDurationBlocked } from "./ui.js";

// WaveSurfer imports (loaded dynamically)
let WaveSurfer = null;
let RegionsPlugin = null;

// State
let trimWs = null;
let trimRegion = null;
let trimJobId = null;
let trimId = null;  // Current trim ID (e.g., "5000_30000")
let trimReady = false;
let trimModal = null;
let regionPlugin = null;
let pendingStart = null;
let zoomLevel = 50;
let isLooping = false;
let isOpening = false;
let trimSession = 0;
let zoomRaf = 0;
let beatGridRaf = 0;
let trimAbortController = new AbortController();
let listenersAttached = false;
let panListenersAttached = false;
let isPanning = false;
let panStartX = 0;
let panScrollLeft = 0;
let movedDuringPan = false;
let trimOverlayLeft = null;
let trimOverlayRight = null;
let lastFocusedBeforeTrimModal = null;
let bpm = null;
let beatOffset = 0;
let beatInterval = null;
let beatGridEl = null;

const ZOOM_BASE = 1.2;
const JOB_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ZOOM_MIN = 20;
const SELECTION_PADDING_FACTOR = 0.15;
const TARGET_VIEWPORT_FILL = 0.9;
const WAVESURFER_MODULE_PATH = "/static/vendor/wavesurfer/dist/wavesurfer.esm.js";
const WAVESURFER_REGIONS_PATH = "/static/vendor/wavesurfer/dist/plugins/regions.esm.js";
const MIN_SELECTION_SECONDS = 1;
const DEFAULT_ZOOM_LEVEL = 50;
const MAX_ZOOM_LEVEL = 2000;
const PAN_THRESHOLD_PX = 4;
const SELECTION_REGION_COLOR = "rgba(99, 102, 241, 0.55)";
const LALAL_REQUEST_TIMEOUT_MS = 60_000;

function isDurationBlocked(jobId) {
    const job = getJobById(jobId);
    return job ? isJobDurationBlocked(job) : false;
}

// DOM Elements (lazy-loaded)
let trimModalEl = null;
let trimWaveEl = null;
let trimInfoEl = null;
let btnPlay = null;
let btnPause = null;
let btnApply = null;
let btnDownload = null;
let btnVocals = null;
let btnInstr = null;
let btnLoop = null;
let loaderEl = null;


function setPlaybackControlsEnabled(enabled) {
    const interactive = Boolean(enabled);
    setButtonState(btnPlay, { disabled: !interactive });
    setButtonState(btnPause, { disabled: !interactive });
    setButtonState(btnLoop, { disabled: !interactive });
}


function getCsrfToken() {
    const token = getCookie(CSRF_COOKIE_NAME);
    if (!token) {
        reportError(new Error(`CSRF token missing from ${CSRF_COOKIE_NAME} cookie`), {
            module: "trim",
            action: "getCsrfToken",
        });
    }
    return token;
}


function requireCsrfToken() {
    const token = getCsrfToken();
    if (!token) {
        throw new Error("CSRF token missing");
    }
    return token;
}


function setButtonState(btn, { disabled, text }) {
    if (!btn) return;
    if (typeof disabled === "boolean") {
        btn.disabled = disabled;
    }
    if (typeof text === "string") {
        btn.textContent = text;
    }
}



function isValidJobId(jobId) {
    return JOB_ID_RE.test(String(jobId ?? ""));
}


function resetButtons() {
    setPlaybackControlsEnabled(false);
    setButtonState(btnApply, { disabled: true, text: "Use Selection" });
    setButtonState(btnVocals, { disabled: true });
    setButtonState(btnInstr, { disabled: true });

    isLooping = false;
    syncLoopButton();

    if (btnDownload) {
        btnDownload.classList.add("d-none");
        btnDownload.href = "#";
    }
}


async function parseApiResponse(res, fallbackMessage) {
    let data;
    try {
        data = await res.json();
    } catch {
        throw new Error(fallbackMessage);
    }

    if (!data || typeof data !== "object") {
        throw new Error(fallbackMessage);
    }

    if (!res.ok || data.ok === false) {
        throw new Error(data.error || data.detail || fallbackMessage);
    }

    return data;
}


function destroyWaveSurfer() {
    if (!trimWs) return;
    trimWs.destroy();
    trimWs = null;
    trimWaveEl?.replaceChildren();
    trimOverlayLeft = null;
    trimOverlayRight = null;
    beatGridEl = null;
}


function resetBeatGridState() {
    bpm = null;
    beatOffset = 0;
    beatInterval = null;
    beatGridEl = null;
}


function applyBeatOptions(options = {}) {
    const rawBpm = Number(options.bpm);
    bpm = Number.isFinite(rawBpm) && rawBpm > 0 ? rawBpm : null;

    const rawBeatOffset = Number(options.beatOffset);
    beatOffset = Number.isFinite(rawBeatOffset) ? rawBeatOffset : 0;
    beatInterval = bpm !== null ? 60 / bpm : null;
}

function normalizeSelectionRange(start, end, duration) {
    return normalizeTimeRange(start, end, duration);
}


function ensureBeatGrid() {
    if (!trimWaveEl) return;

    if (!(beatGridEl instanceof HTMLElement)) {
        beatGridEl = document.createElement("div");
        beatGridEl.className = "beat-grid";
        trimWaveEl.appendChild(beatGridEl);
    }
}


function drawBeatGrid() {
    if (!trimWs || !beatInterval || !trimWaveEl) return;

    const duration = trimWs.getDuration();
    const wrapper = typeof trimWs.getWrapper === "function" ? trimWs.getWrapper() : null;
    const scrollContainer = getWaveScrollContainer();
    if (!(wrapper instanceof HTMLElement) || !(scrollContainer instanceof HTMLElement) || !(duration > 0)) {
        return;
    }

    ensureBeatGrid();
    if (!(beatGridEl instanceof HTMLElement)) return;

    beatGridEl.replaceChildren();

    const totalWidth = wrapper.scrollWidth;
    const viewportWidth = scrollContainer.clientWidth || trimWaveEl.clientWidth;
    if (!(totalWidth > 0) || !(viewportWidth > 0)) {
        return;
    }

    const scrollLeft = scrollContainer.scrollLeft;
    const startTime = (scrollLeft / totalWidth) * duration;
    const endTime = ((scrollLeft + viewportWidth) / totalWidth) * duration;
    let firstBeatIndex = Math.floor((startTime - beatOffset) / beatInterval);
    if (firstBeatIndex < 0) {
        firstBeatIndex = 0;
    }

    const fragment = document.createDocumentFragment();

    for (let beatIndex = firstBeatIndex; ; beatIndex += 1) {
        const time = beatOffset + beatIndex * beatInterval;
        if (time > endTime) {
            break;
        }

        const x = ((time / duration) * totalWidth) - scrollLeft;
        if (x < -2 || x > viewportWidth + 2) {
            continue;
        }

        const line = document.createElement("div");
        line.className = "beat-line";
        if (beatIndex % 4 === 0) {
            line.classList.add("beat-line-strong");
        }
        line.style.left = "0";
        line.style.transform = `translateX(${x}px)`;
        fragment.appendChild(line);
    }

    beatGridEl.appendChild(fragment);
}


function scheduleBeatGridDraw() {
    if (beatGridRaf) return;
    beatGridRaf = requestAnimationFrame(() => {
        beatGridRaf = 0;
        drawBeatGrid();
    });
}


function syncLoopButton() {
    if (!btnLoop) return;

    btnLoop.classList.toggle("active", isLooping);
    btnLoop.setAttribute("aria-pressed", isLooping ? "true" : "false");
    btnLoop.textContent = isLooping ? "Loop ✓" : "Loop";
}


function handleKeydown(e) {
    if (!trimWs || !trimReady) return;
    if (e.target instanceof Element && e.target.closest("button, a, input, select, textarea, [role='button']")) {
        return;
    }

    switch (e.key) {
        case " ":
            e.preventDefault();
            if (trimWs.isPlaying()) {
                trimWs.pause();
            } else if (trimRegion) {
                trimWs.play(trimRegion.start, trimRegion.end);
            }
            break;

        case "l":
        case "L":
            isLooping = !isLooping;
            syncLoopButton();
            showToast(`Loop ${isLooping ? "enabled" : "disabled"}`, "info");
            break;

        case "ArrowLeft":
            trimWs.setTime(Math.max(0, trimWs.getCurrentTime() - SNAP_INTERVAL_SECONDS));
            break;

        case "ArrowRight":
            trimWs.setTime(trimWs.getCurrentTime() + SNAP_INTERVAL_SECONDS);
            break;

        case "Enter":
            handleApplyTrim();
            break;
    }
}


function handleWheelZoom(e) {
    if (!trimWs || !trimReady) return;

    e.preventDefault();

    zoomLevel *= e.deltaY > 0 ? 1 / ZOOM_BASE : ZOOM_BASE;
    zoomLevel = clamp(zoomLevel, 1, MAX_ZOOM_LEVEL);

    cancelAnimationFrame(zoomRaf);
    zoomRaf = requestAnimationFrame(() => {
        trimWs?.zoom(zoomLevel);
    });
}

/**
 * Dynamically load WaveSurfer and RegionsPlugin into module-level globals.
 * Idempotent: returns true immediately if both modules are already loaded.
 * On failure, resets both globals to null, logs the error, and returns false.
 * @returns {Promise<boolean>}
 */
async function loadWaveSurfer() {
    if (WaveSurfer && RegionsPlugin) return true;

    try {
        const wsModule = await import(WAVESURFER_MODULE_PATH);
        WaveSurfer = wsModule.default;

        const regModule = await import(WAVESURFER_REGIONS_PATH);
        RegionsPlugin = regModule.default;

        return true;
    } catch (err) {
        WaveSurfer = null;
        RegionsPlugin = null;
        console.error("Failed to load WaveSurfer:", err);
        return false;
    }
}


function refreshElement(current, id) {
    return current && document.body.contains(current)
        ? current
        : document.getElementById(id);
}

/**
 * Initialize DOM element references
 */
function initElements() {
    trimModalEl = refreshElement(trimModalEl, "trimModal");
    trimWaveEl = refreshElement(trimWaveEl, "trimWave");
    trimInfoEl = refreshElement(trimInfoEl, "trimInfo");
    btnPlay = refreshElement(btnPlay, "trimPlay");
    btnPause = refreshElement(btnPause, "trimPause");
    btnApply = refreshElement(btnApply, "trimApply");
    btnDownload = refreshElement(btnDownload, "trimDownload");
    btnVocals = refreshElement(btnVocals, "trimVocals");
    btnInstr = refreshElement(btnInstr, "trimInstr");
    btnLoop = refreshElement(btnLoop, "trimLoop");
    loaderEl = refreshElement(loaderEl, "trimLoader");

    syncLoopButton();

    if (trimModalEl && !trimModal && typeof bootstrap !== "undefined") {
        trimModal = new bootstrap.Modal(trimModalEl);
    }

    setupEventListeners();
}


function handleVocalsClick() {
    void runLalal("vocals");
}


function handleInstrumentalClick() {
    void runLalal("instrumental");
}


function handleLoopToggle() {
    isLooping = !isLooping;
    syncLoopButton();

    if (isLooping && trimRegion && trimWs) {
        trimWs.play(trimRegion.start, trimRegion.end);
    }
}


function isRegionInteractionEvent(event) {
    const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
    return path.some((node) => {
        if (!(node instanceof Element)) return false;
        const part = node.getAttribute("part") || "";
        return part.includes("region") || part.includes("marker");
    });
}


function getWaveScrollContainer() {
    if (!trimWs) return null;

    const wrapper = typeof trimWs.getWrapper === "function" ? trimWs.getWrapper() : null;
    if (!(wrapper instanceof HTMLElement)) return null;

    return wrapper.parentElement instanceof HTMLElement ? wrapper.parentElement : null;
}


function getWaveClickTime(clientX) {
    if (!trimWs) return null;

    const duration = trimWs.getDuration();
    if (!(duration > 0)) return null;

    const wrapper = typeof trimWs.getWrapper === "function" ? trimWs.getWrapper() : null;
    const scrollContainer = getWaveScrollContainer();
    if (!(scrollContainer instanceof HTMLElement) || !(wrapper instanceof HTMLElement)) return null;

    const rect = scrollContainer.getBoundingClientRect();
    const totalWidth = wrapper.scrollWidth;
    if (!(rect.width > 0) || !(totalWidth > 0)) return null;

    const x = clientX - rect.left;
    const absoluteX = scrollContainer.scrollLeft + x;

    return clamp((absoluteX / totalWidth) * duration, 0, duration);
}


function setSelectionVisualState(hasSelection) {
    trimWaveEl?.classList.toggle("has-selection", hasSelection);
    if (!hasSelection) {
        trimOverlayLeft?.classList.remove("is-visible");
        trimOverlayRight?.classList.remove("is-visible");
    }
}


function ensureOverlays() {
    if (!trimWaveEl) return;

    if (!(trimOverlayLeft instanceof HTMLElement)) {
        trimOverlayLeft = document.createElement("div");
        trimOverlayLeft.className = "trim-overlay trim-overlay-left";
        trimWaveEl.appendChild(trimOverlayLeft);
    }

    if (!(trimOverlayRight instanceof HTMLElement)) {
        trimOverlayRight = document.createElement("div");
        trimOverlayRight.className = "trim-overlay trim-overlay-right";
        trimWaveEl.appendChild(trimOverlayRight);
    }
}


function updateSelectionOverlays(start, end) {
    if (!trimWs || !trimWaveEl) return;

    const wrapper = typeof trimWs.getWrapper === "function" ? trimWs.getWrapper() : null;
    const scrollContainer = getWaveScrollContainer();
    const duration = trimWs.getDuration();
    if (!(scrollContainer instanceof HTMLElement) || !(wrapper instanceof HTMLElement) || !(duration > 0)) return;

    ensureOverlays();
    if (!(trimOverlayLeft instanceof HTMLElement) || !(trimOverlayRight instanceof HTMLElement)) return;

    const totalWidth = wrapper.scrollWidth;
    const viewportWidth = scrollContainer.clientWidth || trimWaveEl.clientWidth;
    if (!(totalWidth > 0) || !(viewportWidth > 0)) return;

    const scrollLeft = scrollContainer.scrollLeft;
    const startPx = ((start / duration) * totalWidth) - scrollLeft;
    const endPx = ((end / duration) * totalWidth) - scrollLeft;
    const visibleStart = clamp(startPx, 0, viewportWidth);
    const visibleEnd = clamp(endPx, 0, viewportWidth);
    const leftWidth = Math.max(0, visibleStart);
    const rightLeft = clamp(visibleEnd, 0, viewportWidth);
    const rightWidth = Math.max(0, viewportWidth - rightLeft);

    trimOverlayLeft.style.width = `${leftWidth}px`;
    trimOverlayRight.style.left = `${rightLeft}px`;
    trimOverlayRight.style.width = `${rightWidth}px`;

    trimOverlayLeft.classList.toggle("is-visible", leftWidth > 0);
    trimOverlayRight.classList.toggle("is-visible", rightWidth > 0);
}


function fitSelectionIntoView(start, end) {
    if (!trimWs || !trimWaveEl) return;

    const duration = trimWs.getDuration();
    const scrollContainer = getWaveScrollContainer();
    if (!(duration > 0) || !(scrollContainer instanceof HTMLElement)) return;

    const selectionDuration = end - start;
    const padding = selectionDuration * SELECTION_PADDING_FACTOR;
    const viewStart = Math.max(0, start - padding);
    const viewEnd = Math.min(duration, end + padding);
    const visibleDuration = Math.max(viewEnd - viewStart, 0.001);
    const viewportWidth = trimWaveEl.clientWidth || scrollContainer.clientWidth || 1;
    const targetPx = viewportWidth * TARGET_VIEWPORT_FILL;
    const pxPerSec = targetPx / visibleDuration;

    zoomLevel = clamp(pxPerSec, ZOOM_MIN, MAX_ZOOM_LEVEL);
    trimWs.zoom(zoomLevel);
    trimWs.setScrollTime(viewStart);
}


function handleWaveReset(event) {
    event?.preventDefault();
    if (!trimWs) return;

    isPanning = false;
    movedDuringPan = false;
    pendingStart = null;
    trimRegion?.remove();
    trimRegion = null;
    trimId = null;
    setSelectionVisualState(false);
    zoomLevel = DEFAULT_ZOOM_LEVEL;
    isLooping = false;
    trimWs.pause();
    trimWs.zoom(DEFAULT_ZOOM_LEVEL);
    trimWs.setTime(0);
    resetButtons();

    if (trimInfoEl) {
        trimInfoEl.textContent = "Click to set start, then click again to set the end.";
    }
}


function handlePanStart(event) {
    if (!trimReady || !trimWs || event.button !== 0) return;
    if (isRegionInteractionEvent(event)) return;

    isPanning = true;
    movedDuringPan = false;
    panStartX = event.clientX;
    panScrollLeft = trimWs.getScroll();
    attachPanListeners();
}


function handlePanMove(event) {
    if (!isPanning || !trimWs || !trimWaveEl) return;

    const dx = event.clientX - panStartX;
    if (!movedDuringPan && Math.abs(dx) < PAN_THRESHOLD_PX) {
        return;
    }

    movedDuringPan = true;
    event.preventDefault();
    trimWaveEl.style.cursor = "grabbing";
    trimWs.setScroll(Math.max(0, panScrollLeft - dx));
    if (trimRegion) {
        updateSelectionOverlays(trimRegion.start, trimRegion.end);
    }
}


function handlePanEnd() {
    if (!trimWaveEl) return;

    const shouldSuppressClick = movedDuringPan;
    isPanning = false;
    trimWaveEl.style.cursor = "";
    detachPanListeners();

    if (shouldSuppressClick) {
        window.setTimeout(() => {
            movedDuringPan = false;
        }, 0);
    }
}


function attachPanListeners() {
    if (panListenersAttached) return;
    panListenersAttached = true;
    window.addEventListener("pointermove", handlePanMove);
    window.addEventListener("pointerup", handlePanEnd);
    window.addEventListener("pointercancel", handlePanEnd);
}


function detachPanListeners() {
    if (!panListenersAttached) return;
    panListenersAttached = false;
    window.removeEventListener("pointermove", handlePanMove);
    window.removeEventListener("pointerup", handlePanEnd);
    window.removeEventListener("pointercancel", handlePanEnd);
}


function removeEventListeners() {
    if (!listenersAttached) return;

    btnPlay?.removeEventListener("click", handlePlay);
    btnPause?.removeEventListener("click", handlePause);
    btnApply?.removeEventListener("click", handleApplyTrim);
    btnVocals?.removeEventListener("click", handleVocalsClick);
    btnInstr?.removeEventListener("click", handleInstrumentalClick);
    btnLoop?.removeEventListener("click", handleLoopToggle);

    trimWaveEl?.removeEventListener("wheel", handleWheelZoom);
    trimWaveEl?.removeEventListener("click", handleWaveClick);
    trimWaveEl?.removeEventListener("pointerdown", handlePanStart);
    trimWaveEl?.removeEventListener("dblclick", handleWaveReset);

    trimModalEl?.removeEventListener("shown.bs.modal", handleModalShown);
    trimModalEl?.removeEventListener("hide.bs.modal", handleModalHide);
    trimModalEl?.removeEventListener("hidden.bs.modal", handleModalHidden);

    listenersAttached = false;
}


function handleWaveClick(e) {
    if (!trimReady || !trimWs || !regionPlugin || !trimWaveEl) return;
    if (movedDuringPan || isRegionInteractionEvent(e)) return;

    const time = getWaveClickTime(e.clientX);
    if (time === null) return;

    const duration = trimWs.getDuration();

    if (pendingStart === null) {
        pendingStart = time;
        trimId = null;
        trimRegion?.remove();
        trimRegion = null;
        setSelectionVisualState(false);
        resetButtons();
        trimWs.pause();
        trimWs.setTime(pendingStart);
        if (trimInfoEl) {
            trimInfoEl.textContent = `Start: ${formatTime(pendingStart)} - now select end`;
        }
        return;
    }

    const { start, end } = normalizeSelectionRange(pendingStart, time, duration);
    pendingStart = null;

    if (!Number.isFinite(start) || !Number.isFinite(end)) {
        return;
    }

    const selectedStart = start;
    const selectedEnd = end;

    if (selectedEnd - selectedStart < MIN_SELECTION_SECONDS) {
        trimRegion?.remove();
        trimRegion = null;
        setSelectionVisualState(false);
        resetButtons();
        if (trimInfoEl) {
            trimInfoEl.textContent = `Selection must be at least ${MIN_SELECTION_SECONDS.toFixed(1)}s. Click to choose a new start.`;
        }
        return;
    }

    trimRegion?.remove();
    trimRegion = regionPlugin.addRegion({
        start: selectedStart,
        end: selectedEnd,
        color: SELECTION_REGION_COLOR,
        drag: true,
        resize: true,
    });

    fitSelectionIntoView(selectedStart, selectedEnd);

    updateInfo();
}


function handleModalShown() {
    document.addEventListener("keydown", handleKeydown);
}


function handleModalHide() {
    const focused = document.activeElement;
    if (focused instanceof HTMLElement && trimModalEl?.contains(focused)) {
        focused.blur();
    }
}


function handleModalHidden() {
    document.removeEventListener("keydown", handleKeydown);
    cleanup();

    if (lastFocusedBeforeTrimModal && document.body.contains(lastFocusedBeforeTrimModal)) {
        lastFocusedBeforeTrimModal.focus();
    }

    lastFocusedBeforeTrimModal = null;
}

/**
 * Setup event listeners for trim controls
 */
function setupEventListeners() {
    if (listenersAttached || !trimModalEl) return;
    listenersAttached = true;

    btnPlay?.addEventListener("click", handlePlay);
    btnPause?.addEventListener("click", handlePause);
    btnApply?.addEventListener("click", handleApplyTrim);
    btnVocals?.addEventListener("click", handleVocalsClick);
    btnInstr?.addEventListener("click", handleInstrumentalClick);
    btnLoop?.addEventListener("click", handleLoopToggle);

    trimWaveEl?.addEventListener("wheel", handleWheelZoom, { passive: false });
    trimWaveEl?.addEventListener("click", handleWaveClick);
    trimWaveEl?.addEventListener("pointerdown", handlePanStart);
    trimWaveEl?.addEventListener("dblclick", handleWaveReset);

    trimModalEl.addEventListener("shown.bs.modal", handleModalShown);
    trimModalEl.addEventListener("hide.bs.modal", handleModalHide);
    trimModalEl.addEventListener("hidden.bs.modal", handleModalHidden);
}

/**
 * Format seconds into m:ss.cc display format (for example 1:05.30).
 * Minutes are not zero-padded; seconds are padded to five characters.
 * @param {number} seconds
 * @returns {string}
 */
function formatTime(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = (seconds % 60).toFixed(2);
    return `${mins}:${secs.padStart(5, "0")}`;
}

/**
 * Update the time info display
 */
function updateInfo() {
    if (!trimRegion || !trimInfoEl) return;
    const duration = trimRegion.end - trimRegion.start;
    trimInfoEl.textContent = `${formatTime(trimRegion.start)} – ${formatTime(trimRegion.end)} (${duration.toFixed(1)}s)`;
}

/**
 * Show/hide loading state
 */
function setLoading(loading) {
    loaderEl?.classList.toggle("d-none", !loading);
    trimWaveEl?.classList.toggle("d-none", loading);
    setPlaybackControlsEnabled(!loading && trimReady);
}

/**
 * Open the trim modal for a specific job.
 * @param {string} jobId - UUID of the job whose audio is loaded.
 * @param {{ bpm?: number, beatOffset?: number }} [options]
 * @returns {Promise<void>}
 */
export async function openTrimModal(jobId, options = {}) {
    if (!isValidJobId(jobId)) {
        showToast("Invalid job ID", "danger");
        return;
    }

    initElements();

    if (!trimModal || !trimWaveEl) {
        showToast("Trim modal not available", "danger");
        return;
    }

    if (isOpening || trimModalEl?.classList.contains("show")) {
        return;
    }

    isOpening = true;
    const session = ++trimSession;

    trimJobId = jobId;
    trimId = null;  // Reset trim ID for new job
    trimReady = false;
    pendingStart = null;
    isLooping = false;
    zoomLevel = DEFAULT_ZOOM_LEVEL;

    // Reset UI state
    resetButtons();
    if (trimInfoEl) trimInfoEl.textContent = "Loading waveform...";

    setLoading(true);
    lastFocusedBeforeTrimModal = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    trimModal.show();

    // Destroy previous instance
    destroyWaveSurfer();
    resetBeatGridState();
    applyBeatOptions(options);
    regionPlugin = null;
    trimRegion = null;

    // Load WaveSurfer dynamically
    const loaded = await loadWaveSurfer();
    if (session !== trimSession) {
        isOpening = false;
        return;
    }

    if (!loaded) {
        setLoading(false);
        showToast("Failed to load waveform library", "danger");
        if (trimInfoEl) trimInfoEl.textContent = "Failed to load waveform library";
        isOpening = false;
        return;
    }

    try {
        if (session !== trimSession) return;

        // Create regions plugin
        regionPlugin = RegionsPlugin.create();

        // Create WaveSurfer instance
        trimWs = WaveSurfer.create({
            container: trimWaveEl,
            waveColor: "rgba(255, 255, 255, 0.12)",
            progressColor: "rgba(255, 255, 255, 0.35)",
            cursorColor: "#f59e0b",
            cursorWidth: 2,
            height: 100,
            barWidth: 2,
            barGap: 1,
            barRadius: 2,
            normalize: true,
            interact: false,
            dragToSeek: false,
            minPxPerSec: DEFAULT_ZOOM_LEVEL,
            autoScroll: false,
            autoCenter: false,
            fetchParams: { credentials: "include" },
            plugins: [regionPlugin]
        });

        // Load the audio file
        const fileUrl = `/audio-source/${encodeURIComponent(jobId)}`;
        if (session !== trimSession) return;
        trimWs.load(fileUrl);

        // Setup ready handler
        trimWs.on("ready", () => {
            if (session !== trimSession) return;

            trimReady = true;
            setLoading(false);
            scheduleBeatGridDraw();

            // No default selection - user must click once for start and again for end
            // Buttons stay disabled until selection is made
            if (trimInfoEl) trimInfoEl.textContent = "Click to set start, then click again to set the end.";
        });

        // Enable buttons when region is created
        regionPlugin.on("region-created", (region) => {
            if (session !== trimSession) return;
            trimRegion = region;
            setSelectionVisualState(true);
            updateSelectionOverlays(region.start, region.end);
            setButtonState(btnApply, { disabled: false });
            updateInfo();
        });

        regionPlugin.on("region-removed", (region) => {
            if (session !== trimSession) return;
            if (region !== trimRegion) return;

            trimRegion = null;
            setSelectionVisualState(false);
        });

        regionPlugin.on("region-update-end", (region) => {
            if (session !== trimSession) return;
            if (region !== trimRegion) return;

            updateSelectionOverlays(region.start, region.end);
            updateInfo();
        });

        // Update info on region change
        regionPlugin.on("region-updated", (region) => {
            if (session !== trimSession) return;
            if (region === trimRegion) {
                updateSelectionOverlays(region.start, region.end);
                updateInfo();
            }
        });

        trimWs.on("scroll", () => {
            if (session !== trimSession) return;
            if (trimRegion) updateSelectionOverlays(trimRegion.start, trimRegion.end);
            scheduleBeatGridDraw();
        });

        trimWs.on("redrawcomplete", () => {
            if (session !== trimSession) return;
            if (trimRegion) updateSelectionOverlays(trimRegion.start, trimRegion.end);
            scheduleBeatGridDraw();
        });

        trimWs.on("timeupdate", () => {
            if (session !== trimSession) return;
            if (!isLooping || !trimRegion) return;

            if (trimWs.getCurrentTime() >= trimRegion.end) {
                trimWs.setTime(trimRegion.start);
            }
        });

        // Handle loading errors
        trimWs.on("error", (err) => {
            if (session !== trimSession) return;
            setLoading(false);
            showToast(`Failed to load audio: ${err?.message || err}`, "danger");
            if (trimInfoEl) trimInfoEl.textContent = "Failed to load audio";
        });

    } catch (err) {
        setLoading(false);
        showToast(`Failed to initialize waveform: ${err?.message || err}`, "danger");
    } finally {
        if (session === trimSession) {
            isOpening = false;
        }
    }
}

/**
 * Play the currently selected region from start to end.
 * No-op if the waveform is not ready or no region has been selected.
 */
function handlePlay() {
    if (!trimReady || !trimWs) return;

    if (trimRegion) {
        trimWs.play(trimRegion.start, trimRegion.end);
        return;
    }

    trimWs.play();
}

/**
 * Pause playback
 */
function handlePause() {
    if (!trimWs) return;
    trimWs.pause();
}

/**
 * Apply trim and prepare for Lalal processing
 */
async function handleApplyTrim() {
    if (!trimJobId) return;
    if (!trimRegion) {
        showToast("Please select a range in the waveform first", "info");
        return;
    }
    const session = trimSession;
    const abortController = trimAbortController;

    const start = trimRegion.start;
    const end = trimRegion.end;
    const duration = end - start;

    // Validate selection
    if (duration < 1) {
        showToast("Selection too short (minimum 1 second)", "warning");
        return;
    }

    if (duration > LALAL_MAX_DURATION_SECONDS) {
        showToast("Selection too long (maximum 10 minutes)", "warning");
        return;
    }

    if (!btnApply) return;

    let csrfToken;
    try {
        csrfToken = requireCsrfToken();
    } catch (err) {
        showToast(err.message, "danger");
        return;
    }

    setButtonState(btnApply, { disabled: true, text: "Processing..." });

    try {
        const res = await fetch(`/api/trim/${encodeURIComponent(trimJobId)}`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken,
            },
            body: JSON.stringify({ start, end }),
            signal: abortController.signal,
        });
        const data = await parseApiResponse(res, "Trim failed");
        if (session !== trimSession) return;

        // Store trim_id for Lalal processing
        trimId = data.trim_id || buildTrimId(start, end);

        // Enable Lalal buttons only when the integration is configured and duration is not blocked.
        const durationBlockedForLalal = isDurationBlocked(trimJobId);
        if (isLalalEnabled() && !durationBlockedForLalal && btnVocals) btnVocals.disabled = false;
        if (isLalalEnabled() && !durationBlockedForLalal && btnInstr) btnInstr.disabled = false;

        // Show and configure download button
        if (btnDownload) {
            btnDownload.href = `/api/trim/${encodeURIComponent(trimJobId)}/${encodeURIComponent(trimId)}/download`;
            btnDownload.classList.remove("d-none");
        }

        const cachedNote = data.cached ? " (cached)" : "";
        showToast(`Trimmed ${duration.toFixed(1)}s of audio${cachedNote}`, "success");
        setButtonState(btnApply, { text: "✓ Ready" });

    } catch (err) {
        if (err?.name === "AbortError") return;
        if (session !== trimSession) return;
        showToast(`Trim failed: ${err.message}`, "danger");
        setButtonState(btnApply, { disabled: false, text: "Use Selection" });
    }
}

/**
 * Run Lalal.ai processing on trimmed audio
 */
async function runLalal(stem) {
    if (!isLalalEnabled()) {
        showToast("Lalal.ai is not connected", "warning");
        return;
    }

    if (isDurationBlocked(trimJobId)) {
        showToast("Track exceeds 10 min — blocked by Duration Guard", "warning");
        return;
    }

    if (!trimJobId || !trimId) {
        showToast("Please trim the audio first", "warning");
        return;
    }

    const session = trimSession;
    const abortController = trimAbortController;
    const progressController = new AbortController();
    const requestController = new AbortController();

    const btn = stem === "vocals" ? btnVocals : btnInstr;
    if (!btn) return;

    let csrfToken;
    try {
        csrfToken = requireCsrfToken();
    } catch (err) {
        showToast(err.message, "danger");
        return;
    }

    const originalText = btn.textContent;
    setButtonState(btn, { disabled: true, text: "Processing..." });

    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
        timedOut = true;
        requestController.abort();
    }, LALAL_REQUEST_TIMEOUT_MS);
    const abortRequest = () => requestController.abort();
    abortController.signal.addEventListener("abort", abortRequest, { once: true });

    // Listen for progress updates
    subscribeToLalalProgress(trimJobId, stem, (stage, progress) => {
        if (session !== trimSession) return;
        btn.textContent = `${stage} ${progress}%`;
    }, progressController.signal);

    try {
        // Build URL with trim_id parameter
        const url = new URL(`/api/lalal/${encodeURIComponent(trimJobId)}`, window.location.origin);
        url.searchParams.set("stem", stem);
        url.searchParams.set("trimmed", "true");
        url.searchParams.set("trim_id", trimId);

        const res = await fetch(
            url.toString(),
            {
                method: "POST",
                headers: {
                    "X-CSRF-Token": csrfToken,
                    "Content-Type": "application/json",
                },
                signal: requestController.signal,
            }
        );
        const data = await parseApiResponse(res, "Lalal failed");
        if (session !== trimSession) return;

        // Download the result
        if (data.download_url && isSafeRedirect(data.download_url)) {
            triggerDownload(data.download_url);
        }

        // Show cached indicator (no API credits used)
        const statusText = data.cached ? "✓ Cached" : "✓ Done";
        setButtonState(btn, { text: statusText });

    } catch (err) {
        if (err?.name === "AbortError" && timedOut) {
            if (session !== trimSession) return;
            showToast("Lalal timed out. Please try again.", "warning");
            setButtonState(btn, { text: originalText, disabled: false });
            return;
        }
        if (err?.name === "AbortError") return;
        if (session !== trimSession) return;
        reportError(err, {
            module: "trim",
            action: "runLalal",
            stem,
        });
        showToast(`Lalal failed: ${err.message}`, "danger");
        setButtonState(btn, { text: originalText, disabled: false });
    } finally {
        window.clearTimeout(timeoutId);
        abortController.signal.removeEventListener("abort", abortRequest);
        progressController.abort();
    }
}

/**
 * Cleanup on modal close
 */
function cleanup() {
    trimSession += 1;
    isOpening = false;
    trimAbortController.abort();
    trimAbortController = new AbortController();
    if (zoomRaf) {
        cancelAnimationFrame(zoomRaf);
        zoomRaf = 0;
    }
    if (beatGridRaf) {
        cancelAnimationFrame(beatGridRaf);
        beatGridRaf = 0;
    }
    detachPanListeners();
    isPanning = false;
    movedDuringPan = false;
    trimRegion?.remove();
    trimRegion = null;
    destroyWaveSurfer();
    trimJobId = null;
    trimId = null;
    trimReady = false;
    regionPlugin = null;
    pendingStart = null;
    resetBeatGridState();
    setSelectionVisualState(false);
    zoomLevel = DEFAULT_ZOOM_LEVEL;
    isLooping = false;
    syncLoopButton();
    setLoading(false);

    resetButtons();
    if (trimInfoEl) trimInfoEl.textContent = "–";

    removeEventListeners();
}

/**
 * Initialize the trim module by attaching a delegated click listener
 * for all `[data-action="open-trim"]` triggers on the document.
 * Safe to call once on page load.
 */
let _trimInitialized = false;

export function initTrim() {
    if (_trimInitialized) return;
    _trimInitialized = true;

    document.addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-action='open-trim']");
        if (!btn) return;

        e.preventDefault();
        const jobId = btn.dataset.jobId;
        if (jobId) {
            const row = btn.closest("tr[data-job-id]");
            const bpmValue = row?.dataset?.bpm ? Number(row.dataset.bpm) : null;
            const beatOffsetValue = btn.dataset.beatOffset ? Number(btn.dataset.beatOffset) : 0;
            await openTrimModal(jobId, { bpm: bpmValue, beatOffset: beatOffsetValue });
        }
    });
}
