//
// app/static/js/errors.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Report a client-side error.
 *
 * The console is the destination on purpose: fetchly is self-hosted and has no
 * error backend to ship reports to. Swap the sink here if one is ever added.
 *
 * @param {unknown} error
 * @param {object} [context] - Plain, enumerable properties only.
 */
export function reportError(error, context = {}) {
    const payload = {
        message: error?.message || String(error),
        stack: error?.stack || "",
        context,
        timestamp: new Date().toISOString(),
    };

    console.error("[fetchly]", payload);
}

/**
 * Report a client-side warning. Console-only, see {@link reportError}.
 *
 * @param {string} message
 * @param {object} [context] - Plain, enumerable properties only. Passing an
 *   Error here logs as `{}`, since its properties are not enumerable.
 */
export function reportWarning(message, context = {}) {
    const payload = {
        message,
        context,
        timestamp: new Date().toISOString(),
    };

    console.warn("[fetchly]", payload);
}