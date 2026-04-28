//
// app/static/js/utils.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * @module utils
 *
 * Shared pure-utility functions used across the TubeYou frontend.
 * All exports are side-effect-free and have no DOM dependencies.
 *
 * Exports:
 *  EMPTY_VALUE          — Canonical placeholder for missing values (en-dash).
 *  YOUTUBE_URL_REGEX    — Regex for YouTube URL validation (no case-folding).
 *  humanSize            — Format byte counts as human-readable strings.
 *  formatDuration       — Format seconds as M:SS or H:MM:SS.
 *  getCookie            — Read a cookie value by name (RFC 6265 compliant).
 *  isValidYouTubeUrl    — Validate YouTube URL format.
 *  extractYouTubeVideoId — Extract the 11-char video ID from a URL.
 *
 * NOTE: Keep YOUTUBE_URL_REGEX in sync with app/main.py:_YOUTUBE_URL_PATTERN.
 * Verified by: app/tests/test_url_validation.py::test_js_python_regex_parity
 */

// Canonical placeholder for missing/invalid values
export const EMPTY_VALUE = "–";  // U+2013 EN DASH

// Regex for YouTube URL validation (exact video ID matching, no case-folding).
// Prevents ReDoS by avoiding .* backtracking in query parsing.
export const YOUTUBE_URL_REGEX = /^https:\/\/(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?(?:[^#]*&)?v=|embed\/|v\/|shorts\/)|youtu\.be\/)[\w-]{11}(?:[?#][^\s]*)?$/;

const SIZE_UNITS = Object.freeze([
    { unit: "TB", divisor: 1_099_511_627_776, precision: 2 },
    { unit: "GB", divisor: 1_073_741_824, precision: 2 },
    { unit: "MB", divisor: 1_048_576, precision: 1 },
    { unit: "KB", divisor: 1_024, precision: 1 },
    { unit: "B", divisor: 1, precision: 0 },
]);

/**
 * Format a byte count as a human-readable string.
 * Mirrors the backend `_filesize` filter in app/main.py.
 * Invalid input returns EMPTY_VALUE (en-dash).
 * @param {number | string | null | undefined} bytes
 * @returns {string}
 */
export function humanSize(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return EMPTY_VALUE;
    if (value === 0) return "0 B";

    for (const { unit, divisor, precision } of SIZE_UNITS) {
        if (value >= divisor) {
            return precision === 0
                ? `${Math.round(value / divisor)} ${unit}`
                : `${(value / divisor).toFixed(precision)} ${unit}`;
        }
    }

    return EMPTY_VALUE;  // Unreachable, but satisfies linter
}

/**
 * Format a duration in seconds as `M:SS` for durations under one hour,
 * or `H:MM:SS` for longer durations.
 * Invalid input returns EMPTY_VALUE (en-dash).
 * @param {number | string | null | undefined} sec
 * @returns {string}
 */
export function formatDuration(sec) {
    if (sec === null || sec === undefined) return EMPTY_VALUE;
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
 * Read a cookie value by name (RFC 6265 compliant).
 * Returns an empty string if the cookie is not found or the name contains
 * illegal characters (=, ;, comma, or whitespace).
 * Cookie values are percent-decoded; malformed encoding is returned raw.
 * @param {string} name
 * @returns {string}
 */
export function getCookie(name) {
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

/**
 * Normalize and validate a YouTube URL (internal use).
 * Returns the trimmed URL if valid, null otherwise.
 * @param {unknown} url
 * @returns {string | null}
 * @private
 */
function normalizeYouTubeUrl(url) {
    if (!url || typeof url !== "string") return null;
    const value = url.trim().toLowerCase();  // Normalize case once
    if (!value || value.length > 2048) return null;
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
 * Returns an empty string if no valid ID is found.
 * Note: Distinct from EMPTY_VALUE (en-dash) — empty string signals
 * "no video ID available", not "data not available".
 * @param {unknown} url
 * @returns {string}
 */
export function extractYouTubeVideoId(url) {
    const value = normalizeYouTubeUrl(url);
    if (!value) return "";

    const VIDEO_ID_REGEX = /^[\w-]{11}$/;  // Validation pattern

    try {
        const parsed = new URL(value);
        const host = parsed.hostname.toLowerCase();

        // youtu.be/VIDEO_ID or youtu.be/VIDEO_ID?params
        if (host.endsWith("youtu.be")) {
            const segments = parsed.pathname.split("/").filter(Boolean);
            const candidate = segments[0];
            return VIDEO_ID_REGEX.test(candidate) ? candidate : "";
        }

        if (!host.endsWith("youtube.com")) return "";

        // watch?v=VIDEO_ID (highest priority)
        const directId = parsed.searchParams.get("v");
        if (directId && VIDEO_ID_REGEX.test(directId)) return directId;

        // embed/VIDEO_ID, v/VIDEO_ID, shorts/VIDEO_ID
        const segments = parsed.pathname.split("/").filter(Boolean);
        const knownPrefixes = new Set(["embed", "v", "shorts"]);
        for (let i = 0; i < segments.length - 1; i++) {
            if (knownPrefixes.has(segments[i])) {
                const candidate = segments[i + 1];
                if (VIDEO_ID_REGEX.test(candidate)) return candidate;
            }
        }

        // Fallback to last segment (handles edge cases)
        const last = segments.at(-1) ?? "";
        return VIDEO_ID_REGEX.test(last) ? last : "";
    } catch {
        return "";
    }
}