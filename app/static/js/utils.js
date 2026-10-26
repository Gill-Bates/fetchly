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
 * - {@link localizeValues}, which rewrites server-rendered UTC times and numbers
 *
 * User-visible numbers and times go through {@link formatNumber} and
 * {@link formatDateTime}: browser locale and time zone, UTC instants in.
 *
 * NOTE: Media URL validation is NOT mirrored here. validate_media_url() in
 * app/utils/platform.py is the single authority; {@link isValidMediaUrl} is a
 * cheap host/scheme preflight only. {@link detectPlatform} stays a UI helper
 * (pills/icons) and {@link normalizeYouTubeUrl}/{@link extractYouTubeVideoId}
 * are YouTube copy-paste/thumbnail conveniences.
 */

export const EMPTY_VALUE = "–";  // U+2013 EN DASH, placeholder for missing values

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
        return { signal: undefined, cleanup() { } };
    }

    if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
        return { signal: AbortSignal.timeout(durationMs), cleanup() { } };
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
        return { signal: undefined, cleanup() { } };
    }
    if (validSignals.length === 1) {
        return { signal: validSignals[0], cleanup() { } };
    }

    if (typeof AbortSignal !== "undefined" && typeof AbortSignal.any === "function") {
        return { signal: AbortSignal.any(validSignals), cleanup() { } };
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

/**
 * Whether a long-running request still belongs to the live trim selection.
 * Compared field by field, so a changed selection (revision, trimId) or a
 * closed/reopened modal (session) each invalidate a pending result.
 * @param {{session: number, revision: number, trimId: string | null}} frozen
 * @param {{session: number, revision: number, trimId: string | null}} live
 * @returns {boolean}
 */
export function isSameTrimSelection(frozen, live) {
    return frozen.session === live.session
        && frozen.revision === live.revision
        && frozen.trimId === live.trimId;
}

/**
 * Restore a Lalal button's idle label. By default only a finished label
 * ("Done"/"Cached") is reset, so a label showing a run's progress is kept;
 * `force` also drops progress text (modal reopen).
 * @param {{dataset: Record<string, string>, textContent: string}} btn
 * @param {{force?: boolean}} [options]
 * @returns {boolean} whether the label was restored
 */
export function resetLalalLabel(btn, { force = false } = {}) {
    if (!btn) return false;
    const finished = btn.dataset.lalalDone === "1";
    delete btn.dataset.lalalDone;
    if ((!finished && !force) || btn.dataset.lalalLabel === undefined) return false;
    btn.textContent = btn.dataset.lalalLabel;
    return true;
}

/**
 * Drop a run's ownership of a stem, but only while it still holds it.
 * @param {Map<string, number>} runs
 * @param {string} stem
 * @param {number} runId
 * @returns {boolean}
 */
export function releaseLalalRun(runs, stem, runId) {
    if (runs.get(stem) !== runId) return false;
    runs.delete(stem);
    return true;
}

export function buildTrimId(start, end) {
    const s = Math.round(start * 1000);
    const e = Math.round(end * 1000);
    return `${s}_${e}`;
}

const SIZE_UNITS = Object.freeze([
    { unit: "TiB", divisor: 1_099_511_627_776, precision: 1 },
    { unit: "GiB", divisor: 1_073_741_824, precision: 1 },
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
 * @param {string | string[] | undefined} [locale] defaults to the browser locale
 * @returns {string}
 */
export function humanSize(bytes, locale = undefined) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return EMPTY_VALUE;
    if (value < 1) return `${formatNumber(0, { locale })} B`;

    for (const { unit, divisor, precision } of SIZE_UNITS) {
        if (value >= divisor) {
            const digits = { locale, minimumFractionDigits: precision, maximumFractionDigits: precision };
            return `${formatNumber(value / divisor, digits)} ${unit}`;
        }
    }

    // Unreachable while SIZE_UNITS ends in a divisor of 1, but callers compare
    // against EMPTY_VALUE - never hand them undefined.
    return EMPTY_VALUE;
}

/**
 * Split a humanSize() result into its number and unit for a stat tile.
 * @param {number | string | null | undefined} bytes
 * @returns {{ value: string, unit: string }}
 */
export function splitSize(bytes, locale = undefined) {
    const rendered = humanSize(bytes, locale);
    const match = /^(.+?)\s+(\S+)$/.exec(rendered);
    return match ? { value: match[1], unit: match[2] } : { value: rendered, unit: "" };
}

/**
 * Format one dashboard stat value for its tile.
 * @param {string} statKey
 * @param {unknown} value
 * @param {string | string[] | undefined} [locale] defaults to the browser locale
 * @returns {{ value: string, unit: string }}
 */
export function statParts(statKey, value, locale = undefined) {
    switch (statKey) {
        case "total_bytes":
            return splitSize(value, locale);
        case "total_jobs":
            return { value: formatNumber(Math.max(0, Math.trunc(Number(value) || 0)), { locale }), unit: "" };
        case "total_minutes":
        case "total_lalal_minutes":
            return {
                value: formatNumber(Math.max(0, Number(value) || 0), { locale, maximumFractionDigits: 1 }),
                unit: "",
            };
        default:
            return { value: String(value ?? 0), unit: "" };
    }
}

/**
 * Format a number with the browser's locale (decimal and grouping separators).
 * Non-finite input returns EMPTY_VALUE.
 * @param {unknown} value
 * @param {Intl.NumberFormatOptions & { locale?: string | string[] }} [options]
 * @returns {string}
 */
export function formatNumber(value, { locale = undefined, ...options } = {}) {
    if (value === null || value === undefined || value === "") return EMPTY_VALUE;
    const number = Number(value);
    if (!Number.isFinite(number)) return EMPTY_VALUE;
    return new Intl.NumberFormat(locale, options).format(number);
}

/**
 * Format a UTC timestamp in the browser's locale and time zone.
 * Unparseable input returns EMPTY_VALUE.
 * @param {unknown} value ISO 8601 or SQLite `YYYY-MM-DD HH:MM:SS` (UTC)
 * @param {Intl.DateTimeFormatOptions & { locale?: string | string[] }} [options]
 * @returns {string}
 */
export function formatDateTime(value, { locale = undefined, ...options } = {}) {
    const date = parseUtcTimestamp(value);
    if (Number.isNaN(date.getTime())) return EMPTY_VALUE;
    const parts = Object.keys(options).some((key) => key !== "timeZone")
        ? options
        : { ...options, dateStyle: "medium", timeStyle: "short" };
    return new Intl.DateTimeFormat(locale, parts).format(date);
}

/**
 * Localize server-rendered values below *root*: `<time data-localize datetime>`
 * (UTC instant) and `[data-localize-number]` (`data-fraction-digits` optional).
 * The server's UTC/plain-number text stays as the no-JS fallback.
 * @param {ParentNode} root
 * @param {{ locale?: string | string[], timeZone?: string }} [options]
 */
export function localizeValues(root, { locale = undefined, timeZone = undefined } = {}) {
    if (typeof root?.querySelectorAll !== "function") return;
    for (const el of root.querySelectorAll("time[data-localize]")) {
        const text = formatDateTime(el.getAttribute("datetime"), { locale, timeZone, dateStyle: "medium", timeStyle: "short" });
        if (text !== EMPTY_VALUE) {
            el.textContent = text;
            el.title = el.getAttribute("datetime");
        }
    }
    for (const el of root.querySelectorAll("[data-localize-number]")) {
        const digits = Number(el.dataset.fractionDigits);
        const fraction = Number.isInteger(digits) && digits >= 0
            ? { minimumFractionDigits: digits, maximumFractionDigits: digits }
            : {};
        const text = formatNumber(el.dataset.localizeNumber, { locale, ...fraction });
        if (text !== EMPTY_VALUE) el.textContent = text;
    }
}

/**
 * Parse a job timestamp. SQLite stores UTC as `YYYY-MM-DD HH:MM:SS` without a
 * zone marker, which `new Date()` would read as local time.
 * @param {unknown} value
 * @returns {Date}
 */
export function parseUtcTimestamp(value) {
    const text = value == null ? "" : String(value).trim();
    const sqliteUtc = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
    return new Date(sqliteUtc.test(text) ? `${text.replace(" ", "T")}Z` : text);
}

/**
 * Format a duration in seconds as `M:SS` for durations under one hour,
 * or `H:MM:SS` for longer durations.
 * Invalid input returns EMPTY_VALUE (en-dash).
 * @param {number | string | null | undefined} sec
 * @returns {string}
 */
export function formatDuration(sec) {
    // Number(null) and Number("") are 0, so an unknown duration would render
    // as a confident "0:00" without this guard - jobs carry null until the
    // source metadata or ffprobe has supplied a length.
    if (sec === null || sec === undefined || sec === "") return EMPTY_VALUE;

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
 * Format a Lalal.ai balance the way Lalal.ai itself does: `261m 30s`.
 * Minutes are fractional (261.5 is 261 minutes and 30 seconds).
 * @param {number | string | null | undefined} minutes - Processing minutes left.
 * @returns {string} The formatted balance, or "" when the balance is unknown.
 */
export function formatLalalMinutes(minutes) {
    if (minutes === null || minutes === undefined || minutes === "") return "";

    const value = Number(minutes);
    if (!Number.isFinite(value) || value < 0) return "";

    const totalSeconds = Math.round(value * 60);
    const wholeMinutes = Math.floor(totalSeconds / 60);
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    return `${wholeMinutes}m ${seconds}s`;
}

/**
 * Read the server-rendered CSRF token without coupling browser code to the
 * middleware's cookie name. The cookie takes precedence; the form/meta value
 * is a fallback for pages that render a token but do not receive the cookie.
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
 * Parse `url` against the current origin and return it only when the
 * resolved origin matches; null for anything unparseable or cross-origin.
 * Shared origin check backing both {@link isSafeSameOriginRedirect} and
 * {@link isSafeRedirect} so "same-origin" has one definition.
 * @param {unknown} url
 * @returns {URL | null}
 * @private
 */
function parseSameOriginUrl(url) {
    if (typeof url !== "string" || !url) return null;
    try {
        const parsed = new URL(url, window.location.origin);
        return parsed.origin === window.location.origin ? parsed : null;
    } catch {
        return null;
    }
}

/**
 * Return whether a navigation target resolves to the current origin.
 * @param {unknown} url
 * @returns {boolean}
 */
export function isSafeSameOriginRedirect(url) {
    return parseSameOriginUrl(url) !== null;
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
 * Cheap client-side preflight: is this plausibly a supported media URL?
 *
 * This is UX only. The authority for which URLs are accepted is
 * validate_media_url() in app/utils/platform.py; the server runs the full
 * per-platform grammar and returns the differentiated error. The frontend
 * deliberately does NOT reproduce that grammar — it only weeds out obvious
 * junk early (non-HTTPS, unknown host, embedded credentials, custom port) so
 * the preview and submit do not fire a doomed request. A URL that clears this
 * preflight may still be rejected by the server, and that server rejection is
 * final.
 * @param {unknown} url
 * @returns {boolean}
 */
export function isValidMediaUrl(url) {
    // detectPlatform() already enforces HTTPS and the known-host allowlist.
    if (detectPlatform(url) === null) return false;

    let parsed;
    try {
        parsed = new URL(String(url).trim().replace(/&amp;/g, "&").replace(/[\u200B-\u200D\uFEFF]/g, ""));
    } catch {
        return false;
    }
    // Submitting these would only earn a server rejection; refuse them here so
    // the obviously-broken cases never leave the browser.
    return !(parsed.username || parsed.password || (parsed.port && parsed.port !== "443"));
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
    const parsed = parseSameOriginUrl(url);
    if (!parsed) return false;

    // restrict to download-only endpoints (avoid action endpoints)
    return parsed.pathname.startsWith("/download/")
        || parsed.pathname.startsWith("/api/lalal/download/");
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

/**
 * Return whether the current browser is iOS/iPadOS Safari.
 * iPadOS reports as "MacIntel" with touch points, so the platform check is
 * required in addition to the userAgent check.
 * @returns {boolean}
 * @private
 */
function isIosBrowser() {
    const nav = globalThis.navigator;
    if (!nav) return false;
    if (/iPad|iPhone|iPod/.test(nav.userAgent || "")) return true;
    return nav.platform === "MacIntel" && Number(nav.maxTouchPoints) > 1;
}

/**
 * Start a download for a same-origin URL.
 *
 * Callers typically invoke this after an `await`, at which point iOS Safari has
 * already dropped the user-gesture context and blocks synthetic anchor clicks.
 * Direct navigation still works there because the download endpoints respond
 * with `Content-Disposition: attachment`.
 * @param {string} url
 */
export function triggerDownload(url) {
    if (isIosBrowser()) {
        window.location.assign(url);
        return;
    }

    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
}
