//
// app/static/js/config.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export const CONFIG = Object.freeze({
    MAX_ROWS: 120,
    PAGE_SIZE: 50,
    SCROLL_OFFSET: 250,
    SSE_RECONNECT_MS: 3000,
});

export const AUDIO_TYPE = "audio";

// Server-owned Lalal.ai processing limit, rendered by base.html.
export const LALAL_MAX_DURATION_SECONDS = Number(
    document.documentElement?.dataset.lalalMaxDurationSeconds,
);
if (!Number.isFinite(LALAL_MAX_DURATION_SECONDS) || LALAL_MAX_DURATION_SECONDS <= 0) {
    throw new Error("Invalid Lalal.ai duration limit in page bootstrap data");
}
export const LALAL_MAX_DURATION_MINUTES = LALAL_MAX_DURATION_SECONDS / 60;

export const DOWNLOADABLE_STATUSES = new Set(["done", "analysis", "analysis_done"]);

export const CANCELLABLE_STATUSES = new Set(["queued", "processing", "downloading", "transcoding"]);

export const TERMINAL_STATUSES = new Set(["done", "analysis_done", "error", "cancelled"]);
