//
// app/static/js/errors.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export function reportError(error, context = {}) {
    const payload = {
        message: error?.message || String(error),
        stack: error?.stack || "",
        context,
        timestamp: new Date().toISOString(),
    };

    console.error("[tubeyou]", payload);
}

export function reportWarning(message, context = {}) {
    const payload = {
        message,
        context,
        timestamp: new Date().toISOString(),
    };

    console.warn("[tubeyou]", payload);
}