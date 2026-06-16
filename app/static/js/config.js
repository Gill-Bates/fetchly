//
// app/static/js/config.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export const CONFIG = Object.freeze({
    MAX_ROWS: 120,
    PAGE_SIZE: 50,
    SCROLL_OFFSET: 250,
    WS_RECONNECT_MS: 3000,
});

export const AUDIO_TYPE = "audio";

export const VIDEO_QUALITY_OPTIONS = Object.freeze([
    Object.freeze({ label: "Max (best available)", value: "max" }),
    Object.freeze({ label: "720p", value: "medium" }),
    Object.freeze({ label: "480p", value: "small" }),
]);

// Maximum track duration Lalal.ai can process (also used as max trim selection length).
export const LALAL_MAX_DURATION_SECONDS = 600;

// CSRF double-submit cookie name — must match app/main.py:_CSRF_COOKIE and middleware/csrf.py.
export const CSRF_COOKIE_NAME = "tubeyou_csrf";

export const DOWNLOADABLE_STATUSES = new Set(["done", "analysis", "analysis_done"]);

export const CANCELLABLE_STATUSES = new Set(["queued", "processing", "downloading", "transcoding"]);

export const TERMINAL_STATUSES = new Set(["done", "analysis", "analysis_done", "error", "cancelled"]);