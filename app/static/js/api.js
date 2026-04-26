//
// app/static/js/api.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { CONFIG } from "./config.js";

async function parseError(response) {
    const data = await response.json().catch(() => ({}));
    return data.detail || `HTTP ${response.status}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    try {
        return await fetch(url, {
            ...options,
            signal: controller.signal,
        });
    } finally {
        clearTimeout(timeoutId);
    }
}

export async function fetchJobs(offset) {
    const res = await fetchWithTimeout(`/api/jobs?offset=${offset}&limit=${CONFIG.PAGE_SIZE}`, {
        credentials: "same-origin",
    }, 10000);

    if (!res.ok) {
        throw new Error(await parseError(res));
    }

    return res.json();
}

export async function submitJob(formData, csrf) {
    const res = await fetchWithTimeout("/api/submit", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
        body: formData,
    }, 15000);

    if (!res.ok) {
        throw new Error(await parseError(res));
    }

    return res.json();
}

export async function fetchVideoInfo(url) {
    const res = await fetchWithTimeout(`/api/info?url=${encodeURIComponent(url)}`, {
        credentials: "same-origin",
    }, 15000);

    if (!res.ok) {
        throw new Error(await parseError(res));
    }

    return res.json();
}