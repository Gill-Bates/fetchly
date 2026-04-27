//
// app/static/js/api.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";

const TIMEOUT_DEFAULT_MS = 10_000;
const TIMEOUT_FETCH_JOBS_MS = 10_000;
const TIMEOUT_VIDEO_INFO_MS = 15_000;
const TIMEOUT_SUBMIT_JOB_MS = 60_000;

/**
 * Extract a readable message from a plain error object.
 * Checks `detail` then `error` then falls back to all values.
 * @param {Record<string, unknown>} obj
 * @returns {string}
 */
function _extractMessages(obj) {
    for (const key of ["detail", "error"]) {
        const val = obj[key];
        if (typeof val === "string") return val;
        // Array.isArray must precede typeof-object to avoid treating arrays as plain objects.
        if (Array.isArray(val)) return val.map(toErrorMessage).filter(Boolean).join("; ") || "";
        if (val != null && typeof val === "object") return toErrorMessage(val);
    }
    const values = Object.values(obj).map(toErrorMessage).filter(Boolean);
    return values.length ? values.join("; ") : "";
}

/**
 * Convert heterogeneous API error payloads into a readable string.
 * @param {unknown} value
 * @returns {string}
 */
export function toErrorMessage(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) {
        return value.map(toErrorMessage).filter(Boolean).join("; ") || "";
    }
    if (typeof value === "object") {
        return _extractMessages(value);
    }
    return String(value);
}

async function parseError(response) {
    const text = await response.text().catch(() => "");
    let data;
    try { data = JSON.parse(text); } catch { data = {}; }
    return toErrorMessage(data) || `HTTP ${response.status}: ${text.slice(0, 200)}`;
}

/**
 * Execute fetch with timeout and optional external cancellation.
 * @param {string} url
 * @param {RequestInit & { signal?: AbortSignal }} [options]
 * @param {number} [timeoutMs]
 * @returns {Promise<Response>}
 */
async function fetchWithTimeout(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    const externalSignal = options?.signal;

    // Bail out immediately — avoids opening a connection for an already-cancelled request.
    if (externalSignal?.aborted) {
        throw new DOMException("Request already aborted", "AbortError");
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    const forwardExternalAbort = () => controller.abort();
    externalSignal?.addEventListener("abort", forwardExternalAbort, { once: true });

    try {
        return await fetch(url, {
            ...options,
            credentials: options.credentials ?? "same-origin",
            signal: controller.signal,
        });
    } catch (error) {
        if (error?.name === "AbortError") {
            if (externalSignal?.aborted) {
                throw error;
            }
            throw new Error(`Request timed out after ${Math.ceil(timeoutMs / 1000)}s`);
        }
        throw error;
    } finally {
        clearTimeout(timeoutId);
        externalSignal?.removeEventListener("abort", forwardExternalAbort);
    }
}

/**
 * Perform an API call and return parsed JSON or throw normalized error.
 * @template T
 * @param {string} url
 * @param {RequestInit & { signal?: AbortSignal }} [options]
 * @param {number} [timeoutMs]
 * @returns {Promise<T>}
 */
async function apiCall(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    const res = await fetchWithTimeout(url, options, timeoutMs);
    if (!res.ok) {
        throw new Error(await parseError(res));
    }
    return res.json();
}

/**
 * Fetch paginated jobs.
 * @param {number|string} offset
 * @param {RequestInit & { signal?: AbortSignal }} [options]
 */
export async function fetchJobs(offset, options = {}) {
    return apiCall(
        `/api/jobs?offset=${encodeURIComponent(String(offset))}&limit=${CONFIG.PAGE_SIZE}`,
        options,
        TIMEOUT_FETCH_JOBS_MS,
    );
}

/**
 * Submit a new processing job.
 * @param {FormData} formData
 * @param {string} csrf
 * @param {RequestInit & { signal?: AbortSignal, headers?: Record<string, string> }} [options]
 */
export async function submitJob(formData, csrf, options = {}) {
    return apiCall(
        "/api/submit",
        {
            method: "POST",
            ...options,
            headers: {
                "X-CSRF-Token": csrf,
                ...(options.headers || {}),
            },
            body: formData,
        },
        TIMEOUT_SUBMIT_JOB_MS,
    );
}

/**
 * Fetch metadata for a YouTube URL.
 * @param {string} url
 * @param {RequestInit & { signal?: AbortSignal }} [options]
 */
export async function fetchVideoInfo(url, options = {}) {
    return apiCall(
        `/api/info?url=${encodeURIComponent(url)}`,
        options,
        TIMEOUT_VIDEO_INFO_MS,
    );
}