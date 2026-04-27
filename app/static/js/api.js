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
 * Convert heterogeneous API error payloads into a readable string.
 * @param {unknown} value
 * @returns {string}
 */
export function toErrorMessage(value) {
    if (value == null) {
        return "";
    }

    if (typeof value === "string") {
        return value;
    }

    if (Array.isArray(value)) {
        return value
            .map((item) => toErrorMessage(item))
            .filter(Boolean)
            .join("; ") || "";
    }

    if (typeof value === "object") {
        if (typeof value.detail === "string") {
            return value.detail;
        }

        if (value.detail && typeof value.detail === "object") {
            return toErrorMessage(value.detail);
        }

        if (Array.isArray(value.detail)) {
            return value.detail
                .map((item) => toErrorMessage(item))
                .filter(Boolean)
                .join("; ") || "";
        }

        if (typeof value.error === "string") {
            return value.error;
        }

        if (value.error && typeof value.error === "object") {
            return toErrorMessage(value.error);
        }

        const values = Object.values(value)
            .map((item) => toErrorMessage(item))
            .filter(Boolean);
        if (values.length > 0) {
            return values.join("; ");
        }

        return "";
    }

    return String(value);
}

async function parseError(response) {
    const data = await response.json().catch(() => ({}));
    return toErrorMessage(data) || `HTTP ${response.status}`;
}

/**
 * Execute fetch with timeout and optional external cancellation.
 * @param {string} url
 * @param {RequestInit & { signal?: AbortSignal }} [options]
 * @param {number} [timeoutMs]
 * @returns {Promise<Response>}
 */
async function fetchWithTimeout(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    const controller = new AbortController();
    const externalSignal = options?.signal;
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    const forwardExternalAbort = () => controller.abort();
    if (externalSignal) {
        if (externalSignal.aborted) {
            controller.abort();
        } else {
            externalSignal.addEventListener("abort", forwardExternalAbort, { once: true });
        }
    }

    try {
        return await fetch(url, {
            ...options,
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
        {
            credentials: "same-origin",
            ...options,
        },
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
            credentials: "same-origin",
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
        {
            credentials: "same-origin",
            ...options,
        },
        TIMEOUT_VIDEO_INFO_MS,
    );
}