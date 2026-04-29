//
// app/static/js/config.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export const CONFIG = {
    MAX_ROWS: 120,
    PAGE_SIZE: 50,
    SCROLL_OFFSET: 250,
    WS_RECONNECT_MS: 3000,
};

export const AUDIO_TYPE = "audio";

export const VIDEO_QUALITY_OPTIONS = Object.freeze([
    Object.freeze({ label: "Max (best available)", value: "max" }),
    Object.freeze({ label: "720p", value: "medium" }),
    Object.freeze({ label: "480p", value: "small" }),
]);

export const DOWNLOADABLE_STATUSES = new Set(["done", "analysis", "analysis_done"]);

export const CANCELLABLE_STATUSES = new Set(["queued", "processing", "downloading", "transcoding"]);

export const TERMINAL_STATUSES = new Set(["done", "analysis_done"]);