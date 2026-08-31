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
 * - {@link getCsrfToken}, which reads the rendered document
 * - {@link isSafeRedirect}, which reads `window.location`
 * - {@link subscribeToLalalProgress}, which subscribes to DOM events
 *
 * NOTE: Keep media URL validation aligned with app/utils/platform.py.
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

function isAbortSignalLike(signal) {
    return Boolean(signal)
        && typeof signal.aborted === "boolean"
        && typeof signal.addEventListener === "function"
        && typeof signal.removeEventListener === "function";
}

export function createTimeoutSignal(timeoutMs) {
    const durationMs = Number(timeoutMs);
    if (!(durationMs > 0)) {
        return { signal: undefined, cleanup() {} };
    }

    if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
        return { signal: AbortSignal.timeout(durationMs), cleanup() {} };
    }

    const controller = new AbortController();
    const timerId = globalThis.setTimeout(() => controller.abort(), durationMs);

    return {
        signal: controller.signal,
        cleanup() {
            globalThis.clearTimeout(timerId);
        },
    };
}

export function combineAbortSignals(signals) {
    const validSignals = (signals || []).filter(isAbortSignalLike);
    if (validSignals.length === 0) {
        return { signal: undefined, cleanup() {} };
    }
    if (validSignals.length === 1) {
        return { signal: validSignals[0], cleanup() {} };
    }

    if (typeof AbortSignal !== "undefined" && typeof AbortSignal.any === "function") {
        return { signal: AbortSignal.any(validSignals), cleanup() {} };
    }

    const controller = new AbortController();
    const listeners = [];

    for (const signal of validSignals) {
        if (signal.aborted) {
            controller.abort();
            break;
        }

        const onAbort = () => controller.abort();
        signal.addEventListener("abort", onAbort, { once: true });
        listeners.push([signal, onAbort]);
    }

    return {
        signal: controller.signal,
        cleanup() {
            for (const [signal, onAbort] of listeners) {
                signal.removeEventListener("abort", onAbort);
            }
        },
    };
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

    s = clamp(snapTime(s), 0, duration);
    e = clamp(snapTime(e), 0, duration);

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
export const YOUTUBE_URL_REGEX = /^https:\/\/(?:www\.|m\.|music\.)?(?:youtube\.com\/(?:watch\?(?:[^#]*&)?v=|embed\/|v\/|shorts\/)|youtu\.be\/)[A-Za-z0-9_-]{11}(?:[?#&][^\s]*)?$/i;

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

const LALAL_PROGRESS_EVENT_NAME = "fetchly:lalal-progress";

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
 * Read the server-rendered CSRF token without coupling browser code to the
 * middleware's cookie name. Forms take precedence to preserve login behavior.
 * @returns {string}
 */
export function getCsrfToken() {
    const cookieName = document.documentElement?.dataset.csrfCookieName || "";
    const cookieToken = readCookie(cookieName);
    return cookieToken
        || document.querySelector('input[name="csrf_token"]')?.value
        || document.querySelector('meta[name="csrf-token"]')?.content
        || "";
}

function readCookie(name) {
    if (!name || /[=;,\s]/.test(name)) return "";

    const prefix = `${name}=`;
    for (const part of document.cookie.split(";")) {
        const trimmed = part.trimStart();
        if (!trimmed.startsWith(prefix)) continue;
        const raw = trimmed.slice(prefix.length);
        try {
            return decodeURIComponent(raw);
        } catch {
            return raw;
        }
    }
    return "";
}

/**
 * Return whether a navigation target resolves to the current origin.
 * @param {unknown} url
 * @returns {boolean}
 */
export function isSafeSameOriginRedirect(url) {
    if (typeof url !== "string" || !url) return false;
    try {
        return new URL(url, window.location.origin).origin === window.location.origin;
    } catch {
        return false;
    }
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
    let parsed;
    try {
        parsed = new URL(value);
    } catch {
        return null;
    }

    if (parsed.protocol !== "https:" || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) {
        return null;
    }

    const host = parsed.hostname.toLowerCase();
    const segments = parsed.pathname.split("/").filter(Boolean);
    const videoId = parsed.searchParams.get("v");
    const validVideoId = (candidate) => typeof candidate === "string" && VIDEO_ID_REGEX.test(candidate);

    if (host === "youtu.be" || host === "www.youtu.be") {
        if (segments.length !== 1 || !validVideoId(segments[0])) return null;
    } else if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) {
        const isWatch = parsed.pathname === "/watch" && validVideoId(videoId);
        const isPathVideo = segments.length === 2
            && YOUTUBE_PATH_PREFIXES.has(segments[0])
            && validVideoId(segments[1]);
        if (!isWatch && !isPathVideo) return null;
    } else {
        return null;
    }

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

// Supported platform identifiers (must match app/utils/platform.py).
export const PLATFORM = Object.freeze({
    YOUTUBE: "youtube",
    TIKTOK: "tiktok",
    INSTAGRAM: "instagram",
    FACEBOOK: "facebook",
});

const PLATFORM_PILL_LABELS = Object.freeze({
    [PLATFORM.YOUTUBE]: "YT",
    [PLATFORM.TIKTOK]: "TikTok",
    [PLATFORM.INSTAGRAM]: "Insta",
    [PLATFORM.FACEBOOK]: "FB",
});

const FACEBOOK_EXACT_HOSTS = Object.freeze(["fb.watch", "www.fb.watch", "fb.gg", "www.fb.gg"]);

/**
 * Detect the platform of a URL purely from its host. No UI toggles.
 * Mirrors detect_platform() in app/utils/platform.py.
 * @param {unknown} url
 * @returns {string|null} one of PLATFORM.* or null when unsupported
 */
export function detectPlatform(url) {
    if (!url || typeof url !== "string") return null;
    const value = url.trim().replace(/&amp;/g, "&").replace(/[\u200B-\u200D\uFEFF]/g, "");
    if (!/^https:\/\//i.test(value)) return null;

    let host;
    try {
        host = new URL(value).hostname.toLowerCase();
    } catch {
        return null;
    }
    if (!host) return null;

    if (host === "youtu.be" || host === "www.youtu.be" || ["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) {
        return PLATFORM.YOUTUBE;
    }
    if (host === "tiktok.com" || host.endsWith(".tiktok.com")) {
        return PLATFORM.TIKTOK;
    }
    if (host === "instagram.com" || host.endsWith(".instagram.com") || host === "instagr.am" || host === "www.instagr.am") {
        return PLATFORM.INSTAGRAM;
    }
    if (host === "facebook.com" || host.endsWith(".facebook.com") || FACEBOOK_EXACT_HOSTS.includes(host)) {
        return PLATFORM.FACEBOOK;
    }
    return null;
}

/**
 * Short pill label (YT / TikTok / Insta / FB) for a platform id, or "" if unknown.
 * @param {unknown} platform
 * @returns {string}
 */
export function platformPillLabel(platform) {
    return PLATFORM_PILL_LABELS[platform] || "";
}

/**
 * Validate whether a URL targets a supported platform
 * (YouTube/TikTok/Instagram/Facebook).
 * YouTube keeps strict video-ID validation; the others require a host match
 * plus a non-trivial path (yt-dlp performs the final resolution).
 * Mirrors validate_media_url() in app/utils/platform.py.
 * @param {unknown} url
 * @returns {boolean}
 */
export function isValidMediaUrl(url) {
    const platform = detectPlatform(url);
    if (platform === PLATFORM.YOUTUBE) {
        return isValidYouTubeUrl(url);
    }
    if (platform === PLATFORM.TIKTOK || platform === PLATFORM.INSTAGRAM || platform === PLATFORM.FACEBOOK) {
        let parsed;
        try {
            parsed = new URL(String(url).trim());
        } catch {
            return false;
        }

        if (parsed.protocol !== "https:" || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) {
            return false;
        }

        const segments = parsed.pathname.split("/").filter(Boolean);
        if (platform === PLATFORM.FACEBOOK) {
            const fbHost = parsed.hostname.toLowerCase();
            const fbCode = /^[A-Za-z0-9_-]+$/;
            // Short share hosts: fb.watch/<code>, fb.gg/<code>
            if (FACEBOOK_EXACT_HOSTS.includes(fbHost)) {
                return segments.length >= 1 && fbCode.test(segments[0]);
            }
            if (segments.length === 0) return false;
            // /watch/?v=<id>, /watch?v=<id>, /video.php?v=<id>
            if (segments[0] === "watch" || segments[0] === "video.php") {
                return /^[0-9]{6,}$/.test(parsed.searchParams.get("v") || "");
            }
            // /reel/<id>
            if (segments[0] === "reel") {
                return segments.length >= 2 && fbCode.test(segments[1]);
            }
            // /share/v/<code>, /share/r/<code>
            if (segments[0] === "share") {
                return segments.length >= 3 && ["v", "r"].includes(segments[1]) && fbCode.test(segments[2]);
            }
            // Page permalinks: /<page>/videos/<id> and /<page>/videos/<slug>/<id>
            return segments.length >= 3
                && segments[1] === "videos"
                && /^[A-Za-z0-9._-]{1,64}$/.test(segments[0])
                && fbCode.test(segments[segments.length - 1]);
        }
        if (platform === PLATFORM.INSTAGRAM) {
            return segments.length === 2
                && ["p", "reel", "tv"].includes(segments[0])
                && /^[A-Za-z0-9_-]+$/.test(segments[1]);
        }

        const host = parsed.hostname.toLowerCase();
        const isLongVideo = segments.length === 3
            && /^@[A-Za-z0-9._]{1,24}$/.test(segments[0])
            && ["video", "photo"].includes(segments[1])
            && /^\d{8,}$/.test(segments[2]);
        const isShareHost = ["vm.tiktok.com", "vt.tiktok.com"].includes(host)
            && segments.length === 1
            && /^[A-Za-z0-9_-]+$/.test(segments[0]);
        const isSharePath = ["tiktok.com", "www.tiktok.com"].includes(host) && segments.length === 2
            && segments[0] === "t"
            && /^[A-Za-z0-9_-]+$/.test(segments[1]);
        return isLongVideo || isShareHost || isSharePath;
    }
    return false;
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
            const progress = clamp(Number(detail.progress), 0, 100);
            onProgress(stageSymbol, Math.round(progress));
        },
        { signal },
    );
}

export function triggerDownload(url) {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
}
