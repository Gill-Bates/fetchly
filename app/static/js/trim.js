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
import { LALAL_MAX_DURATION_MINUTES, LALAL_MAX_DURATION_SECONDS } from "./config.js?v=20260831b";
import {
    buildTrimId,
    clamp,
    getCsrfToken as readCsrfToken,
    isSafeRedirect,
    normalizeTimeRange,
    subscribeToLalalProgress,
    triggerDownload,
    SNAP_INTERVAL_SECONDS,
} from "./utils.js";
import { isLalalEnabled, isLalalDurationGuardEnabled } from "./ui.js?v=20260831c";

// WaveSurfer imports (loaded dynamically)
let WaveSurfer = null;
let RegionsPlugin = null;

// State
let trimWs = null;
let trimRegion = null;
let trimJobId = null;
let trimId = null;  // Current trim ID (e.g., "5000_30000")
let trimDurationSeconds = null;
let selectionRevision = 0;
let trimReady = false;
let trimModal = null;
let regionPlugin = null;
let pendingStart = null;
let zoomLevel = 50;
let isLooping = false;
let isOpening = false;
let trimSession = 0;
let zoomRaf = 0;
let pendingZoomClientX = null;
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
let trimSelectionEl = null;
let trimSelectionStartHandle = null;
let trimSelectionEndHandle = null;
let selectionDragState = null;
let suppressNextWaveClick = false;
let suppressWaveClickTimeout = 0;
let lastFocusedBeforeTrimModal = null;
let bpm = null;
let beatOffset = 0;
let beatInterval = null;
let beatGridEl = null;

const JOB_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ZOOM_WHEEL_SENSITIVITY = 0.0022;
const ZOOM_WHEEL_STEP_MAX = 1.4;
const WHEEL_LINE_HEIGHT_PX = 16;
const WHEEL_PAGE_HEIGHT_PX = 400;
const SELECTION_PADDING_FACTOR = 0.15;
const TARGET_VIEWPORT_FILL = 0.9;
const WAVESURFER_MODULE_PATH = "/static/vendor/wavesurfer/dist/wavesurfer.esm.js";
const WAVESURFER_REGIONS_PATH = "/static/vendor/wavesurfer/dist/plugins/regions.esm.js";
const MIN_SELECTION_SECONDS = 1;
const DEFAULT_SELECTION_SECONDS = 5;
const DEFAULT_ZOOM_LEVEL = 50;
const MAX_ZOOM_LEVEL = 2000;
const PAN_THRESHOLD_PX = 4;
const SELECTION_REGION_COLOR = "transparent";
const TRIM_REQUEST_TIMEOUT_MS = 120_000;
const LALAL_REQUEST_TIMEOUT_MS = 15 * 60_000;
const MIN_BPM = 20;
const MAX_BPM = 400;
const MAX_VISIBLE_BEAT_LINES = 2_000;
let trimRequestBusy = false;

function isTrimDurationBlocked() {
    if (!isLalalDurationGuardEnabled()) {
        return false;
    }

    return !Number.isFinite(trimDurationSeconds)
        || trimDurationSeconds > LALAL_MAX_DURATION_SECONDS;
}

// DOM Elements (lazy-loaded)
let trimModalEl = null;
let trimWaveEl = null;
let trimInfoEl = null;
let btnPlay = null;
let btnPause = null;
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
    const token = readCsrfToken();
    if (!token) {
        reportError(new Error("CSRF token missing from rendered page"), {
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


function resetButtons({ preservePlayback = false } = {}) {
    trimDurationSeconds = null;
    setPlaybackControlsEnabled(preservePlayback && trimReady);
    setButtonState(btnVocals, { disabled: true });
    setButtonState(btnInstr, { disabled: true });

    isLooping = false;
    syncLoopButton();

    if (btnDownload) {
        btnDownload.classList.add("d-none");
        btnDownload.disabled = true;
    }
}


function updateSelectionActions() {
    const hasSelection = trimRegion !== null;
    trimDurationSeconds = hasSelection ? trimRegion.end - trimRegion.start : null;

    if (btnDownload) {
        btnDownload.classList.toggle("d-none", !hasSelection);
        btnDownload.disabled = !hasSelection;
    }

    const canUseLalal = hasSelection && isLalalEnabled() && !isTrimDurationBlocked();
    setButtonState(btnVocals, { disabled: !canUseLalal });
    setButtonState(btnInstr, { disabled: !canUseLalal });
}


function markSelectionChanged() {
    selectionRevision += 1;
    trimId = null;
    updateSelectionActions();
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
    selectionDragState = null;
    if (!trimWs) return;
    trimWs.destroy();
    trimWs = null;
    trimWaveEl?.replaceChildren();
    trimOverlayLeft = null;
    trimOverlayRight = null;
    trimSelectionEl = null;
    trimSelectionStartHandle = null;
    trimSelectionEndHandle = null;
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
    bpm = Number.isFinite(rawBpm) && rawBpm >= MIN_BPM && rawBpm <= MAX_BPM ? rawBpm : null;

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

    let renderedLines = 0;
    for (let beatIndex = firstBeatIndex; renderedLines < MAX_VISIBLE_BEAT_LINES; beatIndex += 1) {
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
        renderedLines += 1;
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

    }
}


/** Wheel deltas arrive in pixels, lines or pages depending on the device. */
function normalizeWheelDelta(delta, deltaMode) {
    if (!Number.isFinite(delta)) return 0;
    if (deltaMode === 1) return delta * WHEEL_LINE_HEIGHT_PX;
    if (deltaMode === 2) return delta * WHEEL_PAGE_HEIGHT_PX;
    return delta;
}


function getWaveViewportWidth() {
    const scrollContainer = getWaveScrollContainer();
    return scrollContainer?.clientWidth || trimWaveEl?.clientWidth || 0;
}


/** Zoom level at which the whole track fills the viewport exactly. */
function getFitZoomLevel() {
    const duration = trimWs?.getDuration?.() || 0;
    const viewportWidth = getWaveViewportWidth();
    if (!(duration > 0) || !(viewportWidth > 0)) return DEFAULT_ZOOM_LEVEL;

    return viewportWidth / duration;
}


function panWaveBy(deltaPx) {
    if (!trimWs || !Number.isFinite(deltaPx) || deltaPx === 0) return;

    const scrollContainer = getWaveScrollContainer();
    if (!(scrollContainer instanceof HTMLElement)) return;

    const maxScroll = Math.max(0, scrollContainer.scrollWidth - scrollContainer.clientWidth);
    if (!(maxScroll > 0)) return;

    trimWs.setScroll(clamp(scrollContainer.scrollLeft + deltaPx, 0, maxScroll));
    lockWaveVerticalScroll();
    if (trimRegion) updateSelectionOverlays(trimRegion.start, trimRegion.end);
    scheduleBeatGridDraw();
}


/**
 * Zoom, move the viewport, then render for where the viewport actually landed.
 *
 * WaveSurfer renders its canvas tiles around the scroll offset that is current
 * when `zoom()` runs, and only fills in missing tiles once the browser delivers
 * the `scroll` event - a frame or more after `setScroll()`/`setScrollTime()`.
 * Zooming into one part of a long track and then jumping the scroll elsewhere
 * therefore leaves the viewport showing tiles rendered for the old offset, or
 * no tiles at all: the waveform blanks out or shows a stale fragment until the
 * next scroll event lands. Repeating the zoom renders the tiles the viewport
 * needs; `minPxPerSec` is unchanged, so width and scroll position survive it.
 *
 * @param {number} nextZoom - Target minPxPerSec.
 * @param {() => void} moveViewport - Applies the scroll jump for this zoom.
 */
function zoomAndMoveViewport(nextZoom, moveViewport) {
    if (!trimWs) return;

    trimWs.zoom(nextZoom);
    moveViewport();
    trimWs.zoom(nextZoom);
}


/**
 * Zoom while keeping the audio position under the pointer pinned in place.
 * WaveSurfer's own reRender() anchors the scroll on the playhead instead,
 * which throws the view back to the start of the track whenever nothing has
 * been played yet - the waveform appears to jump away or vanish.
 * @param {number} nextZoom - Target minPxPerSec.
 * @param {number} [clientX] - Pointer x to keep fixed; viewport center if omitted.
 */
function applyZoomAnchored(nextZoom, clientX) {
    if (!trimWs) return;

    const duration = trimWs.getDuration();
    const wrapper = typeof trimWs.getWrapper === "function" ? trimWs.getWrapper() : null;
    const scrollContainer = getWaveScrollContainer();
    if (!(scrollContainer instanceof HTMLElement) || !(wrapper instanceof HTMLElement) || !(duration > 0)) {
        trimWs.zoom(nextZoom);
        return;
    }

    const rect = scrollContainer.getBoundingClientRect();
    const viewportWidth = scrollContainer.clientWidth || rect.width;
    const widthBefore = wrapper.scrollWidth;
    const pointerX = Number.isFinite(clientX)
        ? clamp(clientX - rect.left, 0, viewportWidth)
        : viewportWidth / 2;
    const anchorTime = widthBefore > 0
        ? clamp(((scrollContainer.scrollLeft + pointerX) / widthBefore) * duration, 0, duration)
        : 0;

    zoomAndMoveViewport(nextZoom, () => {
        const widthAfter = wrapper.scrollWidth;
        if (!(widthAfter > 0)) {
            return;
        }
        const maxScroll = Math.max(0, widthAfter - viewportWidth);
        trimWs.setScroll(clamp(((anchorTime / duration) * widthAfter) - pointerX, 0, maxScroll));
    });

    lockWaveVerticalScroll();
    if (trimRegion) updateSelectionOverlays(trimRegion.start, trimRegion.end);
    scheduleBeatGridDraw();
}


/**
 * Wheel over the waveform: zoom around the pointer, or pan with Shift or a
 * horizontal wheel. Zooming out stops at the full-track view instead of
 * shrinking the waveform into an unreachable sliver.
 */
function handleWheelZoom(e) {
    if (!trimWs || !trimReady) return;

    e.preventDefault();

    const deltaX = normalizeWheelDelta(e.deltaX, e.deltaMode);
    const deltaY = normalizeWheelDelta(e.deltaY, e.deltaMode);

    if (e.shiftKey || Math.abs(deltaX) > Math.abs(deltaY)) {
        panWaveBy(Math.abs(deltaX) > Math.abs(deltaY) ? deltaX : deltaY);
        return;
    }

    // Exponential response keeps a mouse notch and a trackpad swipe comparable,
    // and the clamp stops one large delta from jumping several zoom steps.
    const factor = clamp(
        Math.exp(-deltaY * ZOOM_WHEEL_SENSITIVITY),
        1 / ZOOM_WHEEL_STEP_MAX,
        ZOOM_WHEEL_STEP_MAX,
    );
    const nextZoom = clamp(zoomLevel * factor, getFitZoomLevel(), MAX_ZOOM_LEVEL);
    if (nextZoom === zoomLevel) return;

    zoomLevel = nextZoom;
    pendingZoomClientX = e.clientX;

    cancelAnimationFrame(zoomRaf);
    zoomRaf = requestAnimationFrame(() => {
        zoomRaf = 0;
        applyZoomAnchored(zoomLevel, pendingZoomClientX);
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


function lockWaveVerticalScroll() {
    const scrollContainer = getWaveScrollContainer();
    if (scrollContainer?.scrollTop) {
        scrollContainer.scrollTop = 0;
    }
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
    trimSelectionEl?.classList.toggle("is-visible", hasSelection);
    if (!hasSelection) {
        selectionDragState = null;
        trimOverlayLeft?.classList.remove("is-visible");
        trimOverlayRight?.classList.remove("is-visible");
        trimSelectionEl?.classList.remove("is-visible", "is-dragging");
    }
}


function applySelectionRange(start, end) {
    if (!trimRegion || !trimWs) return;

    const duration = trimWs.getDuration();
    if (!(duration > 0)) return;

    const nextStart = clamp(start, 0, duration);
    const nextEnd = clamp(end, 0, duration);
    if (nextEnd - nextStart < MIN_SELECTION_SECONDS) return;

    trimRegion.setOptions({ start: nextStart, end: nextEnd });
    updateSelectionOverlays(nextStart, nextEnd);
    updateInfo();
    markSelectionChanged();
}


function handleSelectionPointerDown(event) {
    if (!trimRegion || !trimWs || !trimSelectionEl || event.button !== 0) return;

    const pointerTime = getWaveClickTime(event.clientX);
    if (pointerTime === null) return;

    const handle = event.target instanceof Element
        ? event.target.closest(".trim-selection-handle")
        : null;
    const mode = handle?.dataset.selectionHandle || "move";

    selectionDragState = {
        pointerId: event.pointerId,
        pointerTime,
        mode,
        start: trimRegion.start,
        end: trimRegion.end,
    };

    event.preventDefault();
    event.stopPropagation();
    trimSelectionEl.classList.add("is-dragging");
    trimSelectionEl.setPointerCapture(event.pointerId);
}


function handleSelectionPointerMove(event) {
    if (!selectionDragState || !trimRegion || !trimWs) return;
    if (event.pointerId !== selectionDragState.pointerId) return;

    const pointerTime = getWaveClickTime(event.clientX);
    if (pointerTime === null) return;

    event.preventDefault();
    event.stopPropagation();

    if (selectionDragState.mode === "start") {
        const nextStart = clamp(
            pointerTime,
            0,
            trimRegion.end - MIN_SELECTION_SECONDS,
        );
        applySelectionRange(nextStart, trimRegion.end);
        return;
    }

    if (selectionDragState.mode === "end") {
        const nextEnd = clamp(
            pointerTime,
            trimRegion.start + MIN_SELECTION_SECONDS,
            trimWs.getDuration(),
        );
        applySelectionRange(trimRegion.start, nextEnd);
        return;
    }

    const selectionDuration = selectionDragState.end - selectionDragState.start;
    const delta = pointerTime - selectionDragState.pointerTime;
    const nextStart = clamp(
        selectionDragState.start + delta,
        0,
        trimWs.getDuration() - selectionDuration,
    );
    applySelectionRange(nextStart, nextStart + selectionDuration);
}


function handleSelectionPointerEnd(event) {
    if (!selectionDragState || event.pointerId !== selectionDragState.pointerId) return;

    event.preventDefault();
    event.stopPropagation();
    selectionDragState = null;
    trimSelectionEl?.classList.remove("is-dragging");
    if (trimSelectionEl?.hasPointerCapture(event.pointerId)) {
        trimSelectionEl.releasePointerCapture(event.pointerId);
    }
}


function handleSelectionHandleKeydown(event) {
    if (!trimRegion || !trimWs || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;

    const side = event.currentTarget?.dataset?.selectionHandle;
    if (side !== "start" && side !== "end") return;

    event.preventDefault();
    event.stopPropagation();
    const direction = event.key === "ArrowLeft" ? -1 : 1;
    const step = SNAP_INTERVAL_SECONDS * (event.shiftKey ? 10 : 1);

    if (side === "start") {
        const nextStart = clamp(
            trimRegion.start + (direction * step),
            0,
            trimRegion.end - MIN_SELECTION_SECONDS,
        );
        applySelectionRange(nextStart, trimRegion.end);
        return;
    }

    const nextEnd = clamp(
        trimRegion.end + (direction * step),
        trimRegion.start + MIN_SELECTION_SECONDS,
        trimWs.getDuration(),
    );
    applySelectionRange(trimRegion.start, nextEnd);
}


function createSelectionHandle(side) {
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = `trim-selection-handle trim-selection-handle--${side}`;
    handle.dataset.selectionHandle = side;
    handle.setAttribute("role", "slider");
    handle.setAttribute("aria-orientation", "horizontal");
    handle.setAttribute("aria-label", `Adjust selection ${side}`);
    handle.setAttribute("title", `Drag to adjust selection ${side}`);
    handle.addEventListener("keydown", handleSelectionHandleKeydown);
    return handle;
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

    if (!(trimSelectionEl instanceof HTMLElement)) {
        trimSelectionEl = document.createElement("div");
        trimSelectionEl.className = "trim-selection";
        trimSelectionEl.setAttribute("aria-label", "Selected audio range");

        trimSelectionStartHandle = createSelectionHandle("start");
        trimSelectionEndHandle = createSelectionHandle("end");
        trimSelectionEl.append(trimSelectionStartHandle, trimSelectionEndHandle);

        trimSelectionEl.addEventListener("pointerdown", handleSelectionPointerDown);
        trimSelectionEl.addEventListener("pointermove", handleSelectionPointerMove);
        trimSelectionEl.addEventListener("pointerup", handleSelectionPointerEnd);
        trimSelectionEl.addEventListener("pointercancel", handleSelectionPointerEnd);
        trimSelectionEl.addEventListener("click", (event) => event.stopPropagation());
        trimWaveEl.appendChild(trimSelectionEl);
    }
}


function updateSelectionOverlays(start, end) {
    if (!trimWs || !trimWaveEl) return;

    const wrapper = typeof trimWs.getWrapper === "function" ? trimWs.getWrapper() : null;
    const scrollContainer = getWaveScrollContainer();
    const duration = trimWs.getDuration();
    if (!(scrollContainer instanceof HTMLElement) || !(wrapper instanceof HTMLElement) || !(duration > 0)) return;

    lockWaveVerticalScroll();

    ensureOverlays();
    if (
        !(trimOverlayLeft instanceof HTMLElement)
        || !(trimOverlayRight instanceof HTMLElement)
        || !(trimSelectionEl instanceof HTMLElement)
    ) return;

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

    const selectionWidth = Math.max(0, visibleEnd - visibleStart);
    trimSelectionEl.style.left = `${visibleStart}px`;
    trimSelectionEl.style.width = `${selectionWidth}px`;

    trimOverlayLeft.classList.toggle("is-visible", leftWidth > 0);
    trimOverlayRight.classList.toggle("is-visible", rightWidth > 0);
    trimSelectionEl.classList.toggle("is-visible", selectionWidth > 0);

    if (trimSelectionStartHandle instanceof HTMLElement) {
        trimSelectionStartHandle.hidden = startPx < 0 || startPx > viewportWidth;
        trimSelectionStartHandle.setAttribute("aria-valuemin", "0");
        trimSelectionStartHandle.setAttribute("aria-valuemax", String(end));
        trimSelectionStartHandle.setAttribute("aria-valuenow", String(start));
        trimSelectionStartHandle.setAttribute("aria-valuetext", formatTime(start));
    }
    if (trimSelectionEndHandle instanceof HTMLElement) {
        trimSelectionEndHandle.hidden = endPx < 0 || endPx > viewportWidth;
        trimSelectionEndHandle.setAttribute("aria-valuemin", String(start));
        trimSelectionEndHandle.setAttribute("aria-valuemax", String(duration));
        trimSelectionEndHandle.setAttribute("aria-valuenow", String(end));
        trimSelectionEndHandle.setAttribute("aria-valuetext", formatTime(end));
    }
}


function fitSelectionIntoView(start, end) {
    if (!trimWs || !trimWaveEl) return;

    const duration = trimWs.getDuration();
    const scrollContainer = getWaveScrollContainer();
    if (!(duration > 0) || !(scrollContainer instanceof HTMLElement)) return;

    lockWaveVerticalScroll();

    const selectionDuration = end - start;
    const padding = selectionDuration * SELECTION_PADDING_FACTOR;
    const viewStart = Math.max(0, start - padding);
    const viewEnd = Math.min(duration, end + padding);
    const visibleDuration = Math.max(viewEnd - viewStart, 0.001);
    const viewportWidth = trimWaveEl.clientWidth || scrollContainer.clientWidth || 1;
    const targetPx = viewportWidth * TARGET_VIEWPORT_FILL;
    const pxPerSec = targetPx / visibleDuration;

    zoomLevel = clamp(pxPerSec, getFitZoomLevel(), MAX_ZOOM_LEVEL);
    zoomAndMoveViewport(zoomLevel, () => trimWs.setScrollTime(viewStart));
}


function createDefaultSelection() {
    if (!trimWs || !regionPlugin) return;

    const duration = trimWs.getDuration();
    const end = Math.min(DEFAULT_SELECTION_SECONDS, duration);
    if (end < MIN_SELECTION_SECONDS) return;

    trimRegion = regionPlugin.addRegion({
        start: 0,
        end,
        color: SELECTION_REGION_COLOR,
        drag: false,
        resize: false,
    });
    fitSelectionIntoView(0, end);
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
    selectionRevision += 1;
    setSelectionVisualState(false);
    zoomLevel = getFitZoomLevel();
    isLooping = false;
    trimWs.pause();
    trimWs.zoom(zoomLevel);
    trimWs.setTime(0);
    resetButtons({ preservePlayback: true });

    if (trimInfoEl) {
        trimInfoEl.textContent = "Click or tap to set start, then click or tap again to set the end.";
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


/**
 * Touch browsers do not always emit a click after a pointer gesture on the
 * WaveSurfer shadow DOM. Treat a stationary touch release as the equivalent
 * of a click so selecting the start and end works on phones as well.
 */
function handleWavePointerUp(event) {
    if (event.pointerType !== "touch" || movedDuringPan) return;

    suppressNextWaveClick = true;
    window.clearTimeout(suppressWaveClickTimeout);
    suppressWaveClickTimeout = window.setTimeout(() => {
        suppressNextWaveClick = false;
        suppressWaveClickTimeout = 0;
    }, 700);

    handleWaveClick(event, { fromTouchTap: true });
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
    btnDownload?.removeEventListener("click", handleDownloadTrim);
    btnVocals?.removeEventListener("click", handleVocalsClick);
    btnInstr?.removeEventListener("click", handleInstrumentalClick);
    btnLoop?.removeEventListener("click", handleLoopToggle);

    trimWaveEl?.removeEventListener("wheel", handleWheelZoom);
    trimWaveEl?.removeEventListener("click", handleWaveClick);
    trimWaveEl?.removeEventListener("pointerdown", handlePanStart);
    trimWaveEl?.removeEventListener("pointerup", handleWavePointerUp);
    trimWaveEl?.removeEventListener("dblclick", handleWaveReset);

    trimModalEl?.removeEventListener("shown.bs.modal", handleModalShown);
    trimModalEl?.removeEventListener("hide.bs.modal", handleModalHide);
    trimModalEl?.removeEventListener("hidden.bs.modal", handleModalHidden);

    listenersAttached = false;
}


function handleWaveClick(e, { fromTouchTap = false } = {}) {
    if (!fromTouchTap && suppressNextWaveClick) {
        suppressNextWaveClick = false;
        window.clearTimeout(suppressWaveClickTimeout);
        suppressWaveClickTimeout = 0;
        return;
    }

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
        resetButtons({ preservePlayback: true });
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
        resetButtons({ preservePlayback: true });
        if (trimInfoEl) {
            trimInfoEl.textContent = `Selection must be at least ${MIN_SELECTION_SECONDS.toFixed(1)}s. Click or tap to choose a new start.`;
        }
        return;
    }

    trimRegion?.remove();
    trimRegion = regionPlugin.addRegion({
        start: selectedStart,
        end: selectedEnd,
        color: SELECTION_REGION_COLOR,
        drag: false,
        resize: false,
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
    btnDownload?.addEventListener("click", handleDownloadTrim);
    btnVocals?.addEventListener("click", handleVocalsClick);
    btnInstr?.addEventListener("click", handleInstrumentalClick);
    btnLoop?.addEventListener("click", handleLoopToggle);

    trimWaveEl?.addEventListener("wheel", handleWheelZoom, { passive: false });
    trimWaveEl?.addEventListener("click", handleWaveClick);
    trimWaveEl?.addEventListener("pointerdown", handlePanStart);
    trimWaveEl?.addEventListener("pointerup", handleWavePointerUp);
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
 * Show/hide loading state.
 *
 * Only the loader is toggled. #trimWave stays in the layout the whole time -
 * the loader is an overlay on top of it - because WaveSurfer measures the
 * container when it is created and would otherwise render at width 0.
 */
function setLoading(loading) {
    loaderEl?.classList.toggle("d-none", !loading);
    setPlaybackControlsEnabled(!loading && trimReady);
}

/**
 * Resolve once #trimWave has real geometry.
 *
 * Bootstrap only sets the modal to `display: block` after its backdrop
 * transition, so `trimModal.show()` returning does not mean the container has
 * been laid out yet. Creating WaveSurfer against a zero-width container makes
 * its first render bail out (`renderMultiCanvas` computes a chunk width of 0)
 * and seeds its ResizeObserver with width 0, which then fires a second, full
 * re-render roughly 100 ms after the waveform is already on screen.
 *
 * @param {number} [maxFrames] - Frames to wait before giving up.
 * @returns {Promise<boolean>} Whether the container reported a usable width.
 */
async function waitForWaveContainerWidth(maxFrames = 90) {
    for (let frame = 0; frame < maxFrames; frame += 1) {
        if ((trimWaveEl?.clientWidth || 0) > 0) {
            return true;
        }
        await new Promise((resolve) => {
            requestAnimationFrame(() => resolve());
        });
    }
    return (trimWaveEl?.clientWidth || 0) > 0;
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

    await waitForWaveContainerWidth();
    if (session !== trimSession) {
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
            waveColor: "rgba(226, 232, 240, 0.45)",
            progressColor: "rgba(255, 255, 255, 0.7)",
            cursorColor: "#f59e0b",
            cursorWidth: 2,
            height: 100,
            barWidth: 2,
            barGap: 1,
            barRadius: 2,
            barMinHeight: 1,
            normalize: true,
            interact: false,
            dragToSeek: false,
            minPxPerSec: DEFAULT_ZOOM_LEVEL,
            autoScroll: false,
            autoCenter: false,
            fetchParams: { credentials: "include" },
            plugins: [regionPlugin]
        });

        // Setup ready handler
        trimWs.on("ready", () => {
            if (session !== trimSession) return;

            trimReady = true;
            setLoading(false);
            // Start on the full track: minPxPerSec alone would open a long
            // file scrolled into an arbitrary few seconds of audio.
            zoomLevel = getFitZoomLevel();
            trimWs.zoom(zoomLevel);
            lockWaveVerticalScroll();
            scheduleBeatGridDraw();

            // Start with a small, editable range so the common short-clip case
            // needs no extra interaction.
            createDefaultSelection();
        });

        // Enable buttons when region is created
        regionPlugin.on("region-created", (region) => {
            if (session !== trimSession) return;
            trimRegion = region;
            setSelectionVisualState(true);
            updateSelectionOverlays(region.start, region.end);
            setPlaybackControlsEnabled(trimReady);
            markSelectionChanged();
            updateInfo();
        });

        regionPlugin.on("region-removed", (region) => {
            if (session !== trimSession) return;
            if (region !== trimRegion) return;

            trimRegion = null;
            setSelectionVisualState(false);
            markSelectionChanged();
        });

        regionPlugin.on("region-update-end", (region) => {
            if (session !== trimSession) return;
            if (region !== trimRegion) return;

            updateSelectionOverlays(region.start, region.end);
            markSelectionChanged();
            updateInfo();
        });

        // Update info on region change
        regionPlugin.on("region-updated", (region) => {
            if (session !== trimSession) return;
            if (region === trimRegion) {
                updateSelectionOverlays(region.start, region.end);
                markSelectionChanged();
                updateInfo();
            }
        });

        trimWs.on("scroll", () => {
            if (session !== trimSession) return;
            lockWaveVerticalScroll();
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

        // Register all handlers before starting the asynchronous load so an
        // early WaveSurfer failure is observed and its promise is handled.
        const fileUrl = `/audio-source/${encodeURIComponent(jobId)}`;
        if (session !== trimSession) return;
        await trimWs.load(fileUrl);

    } catch (err) {
        if (session !== trimSession) return;
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
 * Materialize the current selection only when a downstream action needs it.
 * The waveform selection remains the sole user-facing confirmation step.
 */
async function ensureTrimSelection() {
    if (trimId) return true;
    if (trimRequestBusy) {
        showToast("Preparing the current selection…", "info");
        return false;
    }
    if (!trimJobId) return false;
    if (!trimRegion) {
        showToast("Please select a range in the waveform first", "info");
        return false;
    }
    const session = trimSession;
    const revision = selectionRevision;
    const abortController = trimAbortController;

    const start = trimRegion.start;
    const end = trimRegion.end;
    const duration = end - start;

    // Validate selection
    if (duration < 1) {
        showToast("Selection too short (minimum 1 second)", "warning");
        return false;
    }

    if (duration > LALAL_MAX_DURATION_SECONDS) {
        showToast(`Selection too long (maximum ${LALAL_MAX_DURATION_MINUTES} minutes)`, "warning");
        return false;
    }

    let csrfToken;
    try {
        csrfToken = requireCsrfToken();
    } catch (err) {
        showToast(err.message, "danger");
        return false;
    }

    setButtonState(btnDownload, { disabled: true });
    setButtonState(btnVocals, { disabled: true });
    setButtonState(btnInstr, { disabled: true });

    trimRequestBusy = true;
    const requestController = new AbortController();
    const timeoutId = window.setTimeout(() => requestController.abort(), TRIM_REQUEST_TIMEOUT_MS);
    const abortRequest = () => requestController.abort();
    abortController.signal.addEventListener("abort", abortRequest, { once: true });

    try {
        const res = await fetch(`/api/trim/${encodeURIComponent(trimJobId)}`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken,
            },
            body: JSON.stringify({ start, end }),
            signal: requestController.signal,
        });
        const data = await parseApiResponse(res, "Trim failed");
        if (session !== trimSession || revision !== selectionRevision) {
            return false;
        }

        // Keep the generated file bound to the exact selection that produced it.
        trimId = data.trim_id || buildTrimId(start, end);
        const normalizedTrimDuration = Number(data.duration);
        trimDurationSeconds = Number.isFinite(normalizedTrimDuration)
            && normalizedTrimDuration >= 0
            ? normalizedTrimDuration
            : null;

        updateSelectionActions();

        const cachedNote = data.cached ? " (cached)" : "";
        showToast(`Trimmed ${duration.toFixed(1)}s of audio${cachedNote}`, "success");
        return true;

    } catch (err) {
        if (err?.name === "AbortError") {
            if (session === trimSession) {
                showToast("Trim timed out. Please try again.", "warning");
                updateSelectionActions();
            }
            return false;
        }
        if (session !== trimSession) return false;
        showToast(`Trim failed: ${err.message}`, "danger");
        updateSelectionActions();
        return false;
    } finally {
        window.clearTimeout(timeoutId);
        abortController.signal.removeEventListener("abort", abortRequest);
        trimRequestBusy = false;
    }
}


async function handleDownloadTrim() {
    if (!await ensureTrimSelection() || !trimJobId || !trimId) return;

    triggerDownload(
        `/api/trim/${encodeURIComponent(trimJobId)}/${encodeURIComponent(trimId)}/download`,
    );
}

/**
 * Run Lalal.ai processing on trimmed audio
 */
async function runLalal(stem) {
    if (!isLalalEnabled()) {
        showToast("Lalal.ai is not connected", "warning");
        return;
    }

    if (!trimJobId) {
        showToast("Please select a range in the waveform first", "warning");
        return;
    }

    if (!await ensureTrimSelection() || !trimId) return;

    if (isTrimDurationBlocked()) {
        showToast(
            trimDurationSeconds === null
                ? "Trim duration is unavailable — blocked by Duration Guard"
                : `Trim duration exceeds ${LALAL_MAX_DURATION_MINUTES} min — blocked by Duration Guard`,
            "warning",
        );
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

        // Show cached indicator (no API credits used). The button stays
        // disabled on purpose: the result belongs to this exact selection, so
        // changing the selection (updateSelectionActions) is what re-arms it.
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
    pendingZoomClientX = null;
    window.clearTimeout(suppressWaveClickTimeout);
    suppressWaveClickTimeout = 0;
    suppressNextWaveClick = false;
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
    trimDurationSeconds = null;
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
            const row = btn.closest("[data-bpm]");
            const bpmValue = row?.dataset?.bpm ? Number(row.dataset.bpm) : null;
            const beatOffsetValue = btn.dataset.beatOffset ? Number(btn.dataset.beatOffset) : 0;
            await openTrimModal(jobId, { bpm: bpmValue, beatOffset: beatOffsetValue });
        }
    });
}
