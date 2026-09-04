//
// tests/js/ui-lint-layout-shift.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Only Chromium implements the `layout-shift` performance entry. Every WebKit
// and Firefox profile therefore reports a CLS of zero, and the single thing
// that must never happen is for that zero to read as "this view is stable".
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    classifyLayoutShift,
    collectLayoutShift,
    LAYOUT_SHIFT_GOOD,
    LAYOUT_SHIFT_POOR,
} = await import("../../tools/ui-lint/lib/layout-shift.mjs");

test("an engine that cannot observe is not credited with a good score", () => {
    assert.equal(classifyLayoutShift({ value: 0, count: 0, supported: false }), "unsupported");
    assert.notEqual(classifyLayoutShift({ value: 0, supported: false }), "good");
});

test("a supported engine with no shifts is good", () => {
    assert.equal(classifyLayoutShift({ value: 0, count: 0, supported: true }), "good");
});

test("classification follows the Core Web Vitals boundaries", () => {
    assert.equal(classifyLayoutShift({ value: LAYOUT_SHIFT_GOOD, supported: true }), "good");
    assert.equal(classifyLayoutShift({ value: LAYOUT_SHIFT_GOOD + 0.01, supported: true }), "needs-improvement");
    assert.equal(classifyLayoutShift({ value: LAYOUT_SHIFT_POOR, supported: true }), "needs-improvement");
    assert.equal(classifyLayoutShift({ value: LAYOUT_SHIFT_POOR + 0.01, supported: true }), "poor");
});

test("an empty argument is unsupported rather than good", () => {
    assert.equal(classifyLayoutShift(), "unsupported");
    assert.equal(classifyLayoutShift({}), "unsupported");
});

test("collection failure degrades to unsupported, never to a clean zero", async () => {
    const page = {
        evaluate() {
            return Promise.reject(new Error("page closed"));
        },
    };

    const result = await collectLayoutShift(page);
    assert.equal(result.supported, false);
    assert.equal(classifyLayoutShift(result), "unsupported");
});
