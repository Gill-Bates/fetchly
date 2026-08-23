//
// app/static/js/boot-flags.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Applies persisted UI flags to <html> before first paint, so the dashboard
// does not flash the default layout before the stored preference is applied.
// Must stay render-blocking (no defer/async) for that reason.
(() => {
    try {
        if (window.localStorage.getItem("tubeyou.showJobHistory") === "true") {
            document.documentElement.classList.add("mobile-job-history-enabled");
        }
    } catch (_) {
        // Storage can be unavailable in private or restricted browser contexts.
    }
})();
