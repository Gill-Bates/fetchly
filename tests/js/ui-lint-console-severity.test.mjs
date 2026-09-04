//
// tests/js/ui-lint-console-severity.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The console allowlist is the one place in the audit where a finding is
// deliberately thrown away. The tests below fix what may be dropped: browser
// noise, and nothing that app/static/js could have caused.
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    CONSOLE_ALLOWLIST,
    filterConsoleEntries,
    scoreConsoleSeverity,
    triageConsoleEntries,
} = await import("../../tools/ui-lint/lib/console-severity.mjs");

test("drops the browser-internal ResizeObserver warning", () => {
    const { kept, suppressed } = filterConsoleEntries([
        { type: "error", text: "ResizeObserver loop completed with undelivered notifications." },
        { type: "error", text: "TypeError: job.filesize_bytes is undefined" },
    ]);

    assert.equal(suppressed.length, 1);
    assert.deepEqual(kept.map((entry) => entry.text), ["TypeError: job.filesize_bytes is undefined"]);
});

test("never allowlists a CSP violation", () => {
    // tests/test_csp_wavesurfer.py exists because a CSP report was once read
    // as noise. The allowlist must not be able to hide one again.
    const cspMessages = [
        "Refused to load the script 'blob:' because it violates the following Content Security Policy directive: \"script-src 'self'\"",
        "Content Security Policy: The page's settings blocked the loading of a resource",
    ];

    for (const text of cspMessages) {
        const { kept } = filterConsoleEntries([{ type: "error", text }]);
        assert.equal(kept.length, 1, `CSP message was suppressed: ${text}`);
    }
});

test("every allowlist pattern names a browser or harness message", () => {
    // A pattern that matches a bare application string would silence real
    // findings, so each one has to be anchored to text only the browser emits.
    const applicationish = [
        "Uncaught TypeError: Cannot read properties of undefined",
        "Failed to fetch /api/jobs",
        "settings: save failed",
    ];

    for (const pattern of CONSOLE_ALLOWLIST) {
        for (const text of applicationish) {
            assert.equal(pattern.test(text), false, `${pattern} swallows application output: ${text}`);
        }
    }
});

test("errors outweigh warnings in the score", () => {
    const errors = scoreConsoleSeverity([{ type: "error", text: "a" }]);
    const warnings = scoreConsoleSeverity([{ type: "warning", text: "b" }]);

    assert.ok(errors.score > warnings.score);
    assert.equal(errors.critical.length, 1);
    assert.equal(warnings.serious.length, 1);
});

test("an unknown message type scores zero rather than throwing", () => {
    const result = scoreConsoleSeverity([{ type: "debug", text: "x" }]);
    assert.equal(result.score, 0);
    assert.equal(result.total, 1);
});

test("triage reports how much it suppressed", () => {
    const result = triageConsoleEntries([
        { type: "error", text: "ResizeObserver loop limit exceeded" },
        { type: "warning", text: "slow network" },
    ]);

    assert.equal(result.suppressed, 1);
    assert.equal(result.entries.length, 1);
    // Only the kept entry contributes; the suppressed one must not be scored.
    assert.equal(result.score, 2);
});
