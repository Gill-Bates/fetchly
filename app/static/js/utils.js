//
// app/static/js/utils.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module utils
 *
 * Shared frontend utility functions.
 *
 * Most exports are side-effect-free and DOM-independent.
 * Exceptions:
 * - {@link getCookie}, which reads `document.cookie`
 * - {@link isSafeRedirect}, which reads `window.location`
 * - {@link subscribeToLalalProgress}, which subscribes to DOM events
 *
 * NOTE: Keep YOUTUBE_URL_REGEX in sync with app/main.py:_YOUTUBE_URL_PATTERN.
 * Prefer {@link isValidYouTubeUrl} and {@link extractYouTubeVideoId} for app logic.
 */

// Canonical placeholder for missing/invalid values
export const EMPTY_VALUE = "–";  // U+2013 EN DASH

// ---------------------------------------------------------------------------
// Time utilities (shared with trim UI)
// ---------------------------------------------------------------------------

export const SNAP_INTERVAL_SECONDS = 0.5;

export function clamp(value, min, max) {
    if (!Number.isFinite(value)) return min;
    return Math.min(max, Math.max(min, value));
}

export function snapTime(seconds, interval = SNAP_INTERVAL_SECONDS) {
    if (!Number.isFinite(seconds) || seconds < 0) return 0;

    const ms = Math.round(seconds * 1000);
    const step = Math.round(interval * 1000);
    if (step <= 0) return seconds;
    return Math.round(ms / step) * step / 1000;
}

/**
 * Normalize a [start, end] range so it stays within [0, duration].
 * If start > end, the values are swapped before snapping.
 * The result is snapped to SNAP_INTERVAL_SECONDS and expanded to a minimal
 * non-zero span when possible.
 * @param {number} start
 * @param {number} end
 * @param {number} duration
 * @returns {{ start: number, end: number }}
 */
export function normalizeTimeRange(start, end, duration) {
    if (!Number.isFinite(duration) || duration <= 0) {
        return { start: 0, end: 0 };
    }

    let s = clamp(start, 0, duration);
    let e = clamp(end, 0, duration);

    if (e < s) [s, e] = [e, s];

    s = snapTime(s);
    e = snapTime(e);

    if (s === e && duration > 0) {
        const interval = SNAP_INTERVAL_SECONDS;
        e = Math.min(duration, s + interval);
        if (e === s) {
            s = Math.max(0, s - interval);
        }
    }

    return { start: s, end: e };
}

export function buildTrimId(start, end) {
    const s = Math.round(start * 1000);
    const e = Math.round(end * 1000);
    return `${s}_${e}`;
}

// Regex for YouTube URL validation (exact video ID matching).
// Exported for parity checks and low-level validation only; callers should prefer
// isValidYouTubeUrl() or extractYouTubeVideoId(), which also normalize input.
export const YOUTUBE_URL_REGEX = /^https?:\/\/(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?(?:[^#]*&)?v=|embed\/|v\/|shorts\/)|youtu\.be\/)[\w-]{11}(?:[?#&][^\s]*)?$/i;

const SIZE_UNITS = Object.freeze([
    { unit: "TiB", divisor: 1_099_511_627_776, precision: 2 },
    { unit: "GiB", divisor: 1_073_741_824, precision: 2 },
    { unit: "MiB", divisor: 1_048_576, precision: 1 },
    { unit: "KiB", divisor: 1_024, precision: 1 },
    { unit: "B", divisor: 1, precision: 0 },
]);

const YOUTUBE_PATH_PREFIXES = new Set(["embed", "v", "shorts"]);

const LALAL_STAGE_SYMBOLS = Object.freeze({
    upload: "↑",
    processing: "⚙",
    download_stem: "↓",
    download_backing: "↓",
});

const LALAL_PROGRESS_EVENT_NAME = "tubeyou:lalal-progress";

/**
 * Format a byte count as a human-readable string.
 * Uses binary prefixes (1024-based).
 * Invalid input returns EMPTY_VALUE (en-dash).
 * @param {number | string | null | undefined} bytes
 * @returns {string}
 */
export function humanSize(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return EMPTY_VALUE;
    if (value < 1) return "0 B";

    for (const { unit, divisor, precision } of SIZE_UNITS) {
        if (value >= divisor) {
            return precision === 0
                ? `${Math.round(value / divisor)} ${unit}`
                : `${(value / divisor).toFixed(precision)} ${unit}`;
        }
    }
}

/**
 * Format a duration in seconds as `M:SS` for durations under one hour,
 * or `H:MM:SS` for longer durations.
 * Invalid input returns EMPTY_VALUE (en-dash).
 * @param {number | string | null | undefined} sec
 * @returns {string}
 */
export function formatDuration(sec) {
    const value = Number(sec);
    if (!Number.isFinite(value) || value < 0) return EMPTY_VALUE;

    const totalSeconds = Math.floor(value);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    const mm = String(minutes).padStart(hours > 0 ? 2 : 1, "0");
    const ss = String(seconds).padStart(2, "0");

    return hours > 0
        ? `${hours}:${mm}:${ss}`
        : `${mm}:${ss}`;
}

/**
 * Read a cookie value by name.
 * Parses `document.cookie` using simple `name=value` splitting.
 * Returns an empty string when the cookie is missing or the name contains
 * illegal characters (`=`, `;`, `,`, or whitespace).
 * Values are percent-decoded; malformed encoding is returned raw.
 * This is not a full RFC 6265 parser: quoted values and duplicate names are
 * not handled specially.
 * @param {string} name
 * @returns {string}
 */
export function getCookie(name) {
    // simple name=value parser (not full RFC 6265 compliant)
    if (!name || /[=;,\s]/.test(name)) return "";

    const prefix = `${name}=`;
    for (const part of document.cookie.split(";")) {
        const trimmed = part.trimStart();
        if (trimmed.startsWith(prefix)) {
            const raw = trimmed.slice(prefix.length);
            try {
                return decodeURIComponent(raw);
            } catch {
                // Malformed encoding: return raw value
                return raw;
            }
        }
    }
    return "";
}

// Shared regex (avoid reallocation)
const VIDEO_ID_REGEX = /^[\w-]{11}$/;

/**
 * Normalize and validate a YouTube URL (internal use).
 * Strips HTML-escaped ampersands and zero-width characters before matching.
 * Returns the trimmed URL if valid, null otherwise.
 * @param {unknown} url
 * @returns {string | null}
 * @private
 */
function normalizeYouTubeUrl(url) {
    if (!url || typeof url !== "string") return null;
    let value = url.trim();

    // Normalize common copy/paste issues
    value = value
        .replace(/&amp;/g, "&")
        .replace(/[\u200B-\u200D\uFEFF]/g, "");

    if (!value || value.length > 2048) return null;
    // Test with case-insensitive check (video IDs are case-sensitive but host is not)
    if (!YOUTUBE_URL_REGEX.test(value)) return null;
    return value;
}

/**
 * Validate whether the input is a supported YouTube URL.
 * @param {unknown} url
 * @returns {boolean}
 */
export function isValidYouTubeUrl(url) {
    return normalizeYouTubeUrl(url) !== null;
}

/**
 * Extract the canonical 11-character YouTube video ID from a supported URL.
 * Strips zero-width characters and HTML entities before parsing.
 * Returns an empty string if no valid ID is found.
 * Note: Distinct from EMPTY_VALUE (en-dash) — empty string signals
 * "no video ID available", not "data not available".
 * @param {unknown} url
 * @returns {string}
 */
export function extractYouTubeVideoId(url) {
    const value = normalizeYouTubeUrl(url);
    if (!value) return "";

    try {
        const parsed = new URL(value);
        const host = parsed.hostname.toLowerCase();

        // strict host validation (prevent phishing domains)
        const isYouTube = host === "youtube.com" || host.endsWith(".youtube.com");
        const isShort = host === "youtu.be" || host.endsWith(".youtu.be");

        if (isShort) {
            const segments = parsed.pathname.split("/").filter(Boolean);
            const candidate = segments[0];
            return VIDEO_ID_REGEX.test(candidate) ? candidate : "";
        }

        if (!isYouTube) return "";

        // watch?v=VIDEO_ID (highest priority)
        const directId = parsed.searchParams.get("v");
        if (directId && VIDEO_ID_REGEX.test(directId)) return directId;

        const segments = parsed.pathname.split("/").filter(Boolean);
        for (let i = 0; i < segments.length - 1; i++) {
            if (YOUTUBE_PATH_PREFIXES.has(segments[i])) {
                const candidate = segments[i + 1];
                if (VIDEO_ID_REGEX.test(candidate)) return candidate;
            }
        }

        return "";
    } catch {
        return "";
    }
}

/**
 * Check if a URL is a safe same-origin redirect to known download paths.
 * Prevents open-redirect attacks by only allowing trusted paths.
 * @param {unknown} url
 * @returns {boolean}
 */
export function isSafeRedirect(url) {
    if (typeof url !== "string") return false;
    try {
        const parsed = new URL(url, window.location.href);
        if (parsed.origin !== window.location.origin) return false;

        // restrict to download-only endpoints (avoid action endpoints)
        return parsed.pathname.startsWith("/download/")
            || parsed.pathname.startsWith("/api/lalal/download/");
    } catch {
        return false;
    }
}

/**
 * Subscribe to Lalal progress events for a specific job/stem pair.
 * Automatically unsubscribes when the provided AbortSignal is aborted.
 * Unknown server stage names fall back to the generic processing symbol.
 * @param {string} jobId
 * @param {string} stem
 * @param {(stageSymbol: string, progress: number) => void} onProgress
 * @param {AbortSignal} signal
 */
export function subscribeToLalalProgress(jobId, stem, onProgress, signal) {
    document.addEventListener(
        LALAL_PROGRESS_EVENT_NAME,
        (event) => {
            const detail = event.detail ?? {};
            if (detail.job_id !== jobId || detail.stem !== stem) return;
            const stageSymbol = LALAL_STAGE_SYMBOLS[detail.stage] ?? "⚙";
            onProgress(stageSymbol, detail.progress);
        },
        { signal },
    );
}