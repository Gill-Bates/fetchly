//
// app/static/js/api.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js?v=20260831b";
import { combineAbortSignals, createTimeoutSignal } from "./utils.js";

const TIMEOUT_DEFAULT_MS = 10_000;
// Kept separate so the jobs polling budget can diverge later without changing all calls.
const TIMEOUT_FETCH_JOBS_MS = TIMEOUT_DEFAULT_MS;
const TIMEOUT_FETCH_STATS_MS = TIMEOUT_DEFAULT_MS;
const TIMEOUT_VIDEO_INFO_MS = 25_000;
const TIMEOUT_THUMBNAIL_MS = 60_000;
const TIMEOUT_SUBMIT_JOB_MS = 60_000;
const RETRYABLE_STATUS_CODES = new Set([502, 503, 504]);
const MAX_ERROR_TEXT_BYTES = 2_048;
const DEFAULT_RETRY_COUNT = 2;
const _TEXT_DECODER = new TextDecoder();
const _inFlightRequests = new Map();

/**
 * @typedef {RequestInit & { signal?: AbortSignal }} ApiOptions
 */

class ApiHttpError extends Error {
    /**
     * @param {number} status
     * @param {string} message
     * @param {string | null} [retryAfter]
     * @param {unknown} [body] Parsed JSON error body, when the response had one.
     */
    constructor(status, message, retryAfter = null, body = null) {
        super(message);
        this.name = "ApiHttpError";
        this.status = status;
        this.retryAfter = retryAfter;
        this.body = body;
    }
}

class ApiTimeoutError extends Error {
    /**
     * @param {number} timeoutMs
     */
    constructor(timeoutMs) {
        super(`Request timed out after ${Math.ceil(timeoutMs / 1000)}s`);
        this.name = "ApiTimeoutError";
        this.timeoutMs = timeoutMs;
    }
}

function _sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function _retryDelayMs(attempt) {
    return 250 * (2 ** attempt);
}

function _formatRetryAfter(retryAfter) {
    if (!retryAfter) {
        return "a moment";
    }

    const numericRetryAfter = Number(retryAfter);
    if (Number.isFinite(numericRetryAfter) && numericRetryAfter > 0) {
        return `${Math.ceil(numericRetryAfter)}s`;
    }

    const retryAt = Date.parse(retryAfter);
    if (!Number.isNaN(retryAt)) {
        const seconds = Math.max(1, Math.ceil((retryAt - Date.now()) / 1000));
        return `${seconds}s`;
    }

    return retryAfter;
}

function _isRetryableFetchError(error) {
    return error instanceof TypeError || error instanceof ApiTimeoutError;
}

function _isRetryableStatus(status) {
    return RETRYABLE_STATUS_CODES.has(status);
}

function _normalizeHeaders(headers) {
    if (!headers) {
        return [];
    }

    const normalized = headers instanceof Headers ? headers : new Headers(headers);
    return [...normalized.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function _requestKey(url, options = {}, timeoutMs) {
    const method = String(options.method ?? "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
        throw new Error(`_apiCallDeduped only supports GET/HEAD requests, got ${method}`);
    }

    return JSON.stringify({
        method,
        url,
        timeoutMs,
        headers: _normalizeHeaders(options.headers),
        credentials: options.credentials,
        cache: options.cache,
        mode: options.mode,
        redirect: options.redirect,
        referrer: options.referrer,
        referrerPolicy: options.referrerPolicy,
        integrity: options.integrity,
        keepalive: options.keepalive,
    });
}

function _waitForAbort(signal) {
    let onAbort = null;
    const promise = new Promise((_, reject) => {
        if (signal.aborted) {
            reject(new DOMException("Request already aborted", "AbortError"));
            return;
        }

        onAbort = () => {
            reject(new DOMException("Request aborted", "AbortError"));
        };

        signal.addEventListener("abort", onAbort, { once: true });
    });

    return {
        promise,
        cleanup: () => {
            if (onAbort) {
                signal.removeEventListener("abort", onAbort);
            }
        },
    };
}

/**
 * Extract a human-readable error message from a JSON error response object.
 *
 * Priority order:
 * 1. `msg` (FastAPI validation error item: { loc, msg, type })
 * 2. `detail` (FastAPI convention)
 * 3. `error` (application convention)
 * 4. Concatenation of all object values as a lossy fallback
 * @param {Record<string, unknown>} obj
 * @returns {string} Empty string if no readable message is present.
 */
function _extractMessages(obj) {
    // Pydantic/FastAPI 422 items carry loc/type noise alongside msg; the lossy
    // fallback below would stringify all of them into an unreadable message.
    if (typeof obj.msg === "string" && obj.msg) return obj.msg;

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

/**
 * @param {Response} response
 * @returns {Promise<{ message: string, data: unknown }>}
 */
async function parseError(response) {
    const text = await _readResponseSnippet(response, MAX_ERROR_TEXT_BYTES);
    let data;
    try {
        data = JSON.parse(text);
    } catch {
        data = {};
    }

    const message = toErrorMessage(data);
    if (message) {
        return { message, data };
    }

    const statusText = response.statusText ? `: ${response.statusText}` : "";
    return {
        message: text ? `HTTP ${response.status}${statusText}: ${text.slice(0, 200)}` : `HTTP ${response.status}${statusText}`,
        data,
    };
}

async function _readResponseSnippet(response, maxBytes = MAX_ERROR_TEXT_BYTES) {
    const reader = response.body?.getReader();
    if (!reader) {
        return "";
    }

    const collected = new Uint8Array(maxBytes);
    let offset = 0;

    try {
        while (offset < maxBytes) {
            const { done, value } = await reader.read();
            if (done || !value) {
                break;
            }

            const remaining = maxBytes - offset;
            const chunk = value.byteLength > remaining ? value.subarray(0, remaining) : value;
            collected.set(chunk, offset);
            offset += chunk.byteLength;
        }

        return _TEXT_DECODER.decode(collected.subarray(0, offset));
    } catch {
        return "";
    } finally {
        try {
            await reader.cancel();
        } catch {
            // Ignore cancellation failures when the body is already closed.
        }
    }
}

function _cancelResponseBody(response) {
    if (!response.body) {
        return;
    }

    void response.body.cancel().catch(() => {
        // Ignore cancellation failures when the body is already closed.
    });
}

/**
 * Fetch a URL with a timeout while propagating external cancellation.
 *
 * Rejects immediately if `options.signal` is already aborted.
 * Throws `ApiTimeoutError` when the timeout expires.
 * Propagates external `AbortError` instances unchanged.
 * @param {string} url
 * @param {ApiOptions} [options]
 * @param {number} [timeoutMs] - Timeout in milliseconds for a single attempt.
 * @returns {Promise<Response>}
 * @throws {ApiTimeoutError} When timeoutMs is exceeded.
 * @throws {DOMException} When `options.signal` aborts the request.
 */
async function fetchWithTimeout(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    const externalSignal = options.signal;

    // Bail out immediately — avoids opening a connection for an already-cancelled request.
    if (externalSignal?.aborted) {
        throw new DOMException("Request already aborted", "AbortError");
    }

    const { signal: timeoutSignal, cleanup: cleanupTimeout } = createTimeoutSignal(timeoutMs);
    const { signal, cleanup: cleanupSignal } = combineAbortSignals([externalSignal, timeoutSignal]);

    try {
        return await fetch(url, {
            ...options,
            credentials: options.credentials ?? "same-origin",
            signal,
        });
    } catch (error) {
        if (error?.name === "AbortError" || error?.name === "TimeoutError") {
            if (externalSignal?.aborted) {
                throw error;
            }
            if (timeoutSignal.aborted) {
                throw new ApiTimeoutError(timeoutMs);
            }
        }
        throw error;
    } finally {
        cleanupSignal();
        cleanupTimeout();
    }
}

/**
 * Core API request helper with normalized timeout, retry, and HTTP error handling.
 * @template T
 * @param {string} url
 * @param {ApiOptions} [options]
 * @param {number} [timeoutMs] - Per-attempt timeout in milliseconds.
 * @param {number} [retries] - Maximum number of additional attempts after the
 *   first failure. Total attempts = retries + 1.
 * @returns {Promise<T>} Parsed JSON response body.
 * @throws {ApiHttpError} On non-retryable HTTP errors.
 * @throws {ApiTimeoutError} When a request attempt exceeds timeoutMs.
 * @throws {DOMException} When the caller aborts via options.signal.
 */
async function _apiCall(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS, retries = 0) {
    let lastError;

    for (let attempt = 0; attempt <= retries; attempt += 1) {
        try {
            const res = await fetchWithTimeout(url, options, timeoutMs);
            if (res.ok) {
                return res.json();
            }

            if (res.status === 429) {
                _cancelResponseBody(res);
                throw new ApiHttpError(
                    429,
                    `Rate limited. Try again in ${_formatRetryAfter(res.headers.get("Retry-After"))}.`,
                    res.headers.get("Retry-After"),
                );
            }

            if (_isRetryableStatus(res.status) && attempt < retries && !options.signal?.aborted) {
                _cancelResponseBody(res);
                await _sleep(_retryDelayMs(attempt));
                continue;
            }

            {
                const { message, data } = await parseError(res);
                throw new ApiHttpError(res.status, message, null, data);
            }
        } catch (error) {
            lastError = error;
            if (_isRetryableFetchError(error) && attempt < retries && !options.signal?.aborted) {
                await _sleep(_retryDelayMs(attempt));
                continue;
            }
            throw error;
        }
    }

    throw lastError;
}

/**
 * Single-attempt API call without automatic retry.
 * Use for non-idempotent requests where retry would be unsafe.
 * @param {string} url
 * @param {ApiOptions} [options]
 * @param {number} [timeoutMs]
 * @returns {Promise<unknown>}
 */
async function apiCall(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    return _apiCall(url, options, timeoutMs, 0);
}

/**
 * Retrying API call helper for idempotent requests.
 * Uses the module default retry budget.
 * @param {string} url
 * @param {ApiOptions} [options]
 * @param {number} [timeoutMs]
 * @returns {Promise<unknown>}
 */
async function _apiCallWithRetry(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    return _apiCall(url, options, timeoutMs, DEFAULT_RETRY_COUNT);
}

async function _raceWithAbort(promise, signal) {
    if (!signal) {
        return promise;
    }

    const abort = _waitForAbort(signal);
    try {
        return await Promise.race([promise, abort.promise]);
    } finally {
        abort.cleanup();
    }
}

/**
 * Request deduplication layer for idempotent GET/HEAD requests.
 *
 * Prevents overlapping identical reads from:
 * - double-clicks
 * - concurrent dashboard refreshes
 * - repeated UI state sync during the same render window
 *
 * Callers share the same underlying network promise. Local abort signals only
 * short-circuit the awaiting caller; they do not cancel the shared request for
 * other consumers.
 * @param {string} url
 * @param {ApiOptions} [options]
 * @param {number} [timeoutMs]
 * @returns {Promise<unknown>}
 */
async function _apiCallDeduped(url, options = {}, timeoutMs = TIMEOUT_DEFAULT_MS) {
    const { signal: callerSignal, ...sharedOptions } = options;
    const key = _requestKey(url, sharedOptions, timeoutMs);
    const cachedPromise = _inFlightRequests.get(key);
    if (cachedPromise) {
        return _raceWithAbort(cachedPromise, callerSignal);
    }

    const sharedPromise = _apiCallWithRetry(url, sharedOptions, timeoutMs).then(
        (result) => {
            _inFlightRequests.delete(key);
            return result;
        },
        (error) => {
            _inFlightRequests.delete(key);
            throw error;
        },
    );
    _inFlightRequests.set(key, sharedPromise);

    return _raceWithAbort(sharedPromise, callerSignal);
}

/**
 * Fetch paginated jobs.
 * @param {number|string} offset
 * @param {ApiOptions} [options]
 */
export async function fetchJobs(offset, options = {}) {
    return _apiCallDeduped(
        `/api/jobs?offset=${encodeURIComponent(String(offset))}&limit=${CONFIG.PAGE_SIZE}`,
        options,
        TIMEOUT_FETCH_JOBS_MS,
    );
}

/**
 * Fetch a single job snapshot.
 * @param {string} jobId
 * @param {ApiOptions} [options]
 */
export async function fetchJob(jobId, options = {}) {
    return _apiCallDeduped(
        `/api/jobs/${encodeURIComponent(String(jobId))}`,
        options,
        TIMEOUT_FETCH_JOBS_MS,
    );
}

/**
 * Fetch fresh dashboard hero stats.
 * @param {ApiOptions} [options]
 */
export async function fetchStats(options = {}) {
    return _apiCallDeduped(
        "/api/stats",
        options,
        TIMEOUT_FETCH_STATS_MS,
    );
}

/**
 * Submit a new processing job.
 * @param {FormData} formData
 * @param {string} csrf
 * @param {ApiOptions} [options]
 */
export async function submitJob(formData, csrf, options = {}) {
    const headers = new Headers(options.headers);
    headers.set("X-CSRF-Token", csrf);

    return apiCall(
        "/api/submit",
        {
            ...options,
            method: "POST",
            headers,
            body: formData,
        },
        TIMEOUT_SUBMIT_JOB_MS,
    );
}

/**
 * Fetch metadata for a supported media URL.
 * @param {string} url
 * @param {ApiOptions} [options]
 */
export async function fetchVideoInfo(url, options = {}) {
    return _apiCallDeduped(
        `/api/info?url=${encodeURIComponent(url)}`,
        options,
        TIMEOUT_VIDEO_INFO_MS,
    );
}

/**
 * Resolve a media URL to a local cached thumbnail URL.
 * @param {string} url
 * @param {ApiOptions} [options]
 */
export async function fetchResolvedThumbnail(url, options = {}) {
    return _apiCallDeduped(
        `/api/thumbnail/resolve?url=${encodeURIComponent(url)}`,
        options,
        TIMEOUT_THUMBNAIL_MS,
    );
}
