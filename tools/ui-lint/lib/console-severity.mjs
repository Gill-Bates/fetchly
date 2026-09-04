//
// tools/ui-lint/lib/console-severity.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Console output triage. Before this module the runner pushed every captured
// console entry into `warnings` verbatim, so a single repeated browser-internal
// message could bury a real pageerror in the report. Two steps now sit in
// between: an allowlist that drops provably environmental noise, and a
// severity score so the remainder can be weighted by the health report.
//

/**
 * Patterns for console output that is not an application defect.
 *
 * The bar for adding an entry is deliberately high: the message must be
 * produced by the browser or the harness itself, never by app/static/js. CSP
 * violations, failed same-origin requests and pageerrors are real findings and
 * must never be listed here - tests/test_csp_wavesurfer.py exists precisely
 * because one of those was once dismissed as noise.
 */
export const CONSOLE_ALLOWLIST = [
    // Fired by the browser when a ResizeObserver callback schedules work that
    // the next frame cannot deliver. Benign on its own; the health report
    // re-escalates it when it coincides with overflow or layout shift.
    /ResizeObserver loop (?:limit exceeded|completed with undelivered notifications)/i,
    // The audit runs without a favicon request path in some contexts.
    /Failed to load resource.*favicon/i,
    // Chromium emits this for missing sourcemaps of vendored bundles.
    /DevTools failed to load (?:source ?map|SourceMap)/i,
];

/**
 * Joins the fields a console entry may carry so one pattern can match against
 * the message, its origin, or its stack.
 * @param {object} entry
 * @returns {string}
 */
function getConsoleSignature(entry) {
    return [entry?.text, entry?.url, entry?.sourceURL, entry?.location, entry?.stack]
        .filter(Boolean)
        .map((value) => String(value))
        .join('\n');
}

/**
 * Drops allowlisted entries and returns both halves, so the report can still
 * say how much was suppressed instead of silently shrinking.
 * @param {object[]} entries
 * @param {{allowlist?: RegExp[]}} [options]
 * @returns {{kept: object[], suppressed: object[]}}
 */
export function filterConsoleEntries(entries = [], { allowlist = CONSOLE_ALLOWLIST } = {}) {
    const kept = [];
    const suppressed = [];

    for (const entry of entries) {
        const signature = getConsoleSignature(entry);
        if (allowlist.some((pattern) => pattern.test(signature))) {
            suppressed.push(entry);
        } else {
            kept.push(entry);
        }
    }

    return { kept, suppressed };
}

/** Points a console entry contributes, by its Playwright message type. */
export const CONSOLE_SEVERITY = Object.freeze({
    error: 3,
    warning: 2,
    info: 1,
    log: 0,
});

/**
 * Buckets console entries by severity and returns a weighted score.
 * The collector in browser-utils.mjs keeps only 'error' and 'warning', so in
 * practice entries land in `critical` and `serious`; the lower buckets exist
 * for callers that widen the collector.
 * @param {object[]} entries
 * @returns {{score: number, total: number, critical: object[], serious: object[], minor: object[]}}
 */
export function scoreConsoleSeverity(entries = []) {
    const critical = [];
    const serious = [];
    const minor = [];
    let score = 0;

    for (const entry of entries) {
        const weight = CONSOLE_SEVERITY[entry?.type] ?? 0;
        score += weight;

        if (weight >= 3) {
            critical.push(entry);
        } else if (weight >= 2) {
            serious.push(entry);
        } else if (weight >= 1) {
            minor.push(entry);
        }
    }

    return { score, total: entries.length, critical, serious, minor };
}

/**
 * Filters and scores in one call - the shape the runner consumes.
 * @param {object[]} entries
 * @param {{allowlist?: RegExp[]}} [options]
 * @returns {object}
 */
export function triageConsoleEntries(entries = [], options = {}) {
    const { kept, suppressed } = filterConsoleEntries(entries, options);
    return { ...scoreConsoleSeverity(kept), entries: kept, suppressed: suppressed.length };
}
