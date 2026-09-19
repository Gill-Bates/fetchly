//
// tests/js/ui-lint-result-summary.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// formatResultSummary reads the metrics exactly as run-ui-lint.mjs persists
// them into results.json: element lists collapsed to counts. Treating a count
// as a list (`.length` on a number) drops the finding from the console line
// while the failure it belongs to is still counted, so the summary and the
// verdict disagree.
//

import assert from "node:assert/strict";
import test from "node:test";

const { formatResultSummary } = await import("../../tools/ui-lint/run-ui-lint.mjs");

const summarize = (metrics) => formatResultSummary({ failures: [], warnings: [], metrics });

test("count metrics are printed with their number", () => {
    const line = summarize({
        flexMinHeightOverflowHidden: 3,
        iosInputZoomTargets: 2,
        viewportUnitTraps: 4,
        bottomPinnedWithoutSafeArea: 1,
    });
    assert.match(line, /flexMinHeightOverflowHidden=3/);
    assert.match(line, /iosInputZoom=2/);
    assert.match(line, /vhWithoutDvh=4/);
    assert.match(line, /bottomPinnedNoSafeArea=1/);
});

test("zero counts stay out of the summary", () => {
    const line = summarize({
        flexMinHeightOverflowHidden: 0,
        iosInputZoomTargets: 0,
        viewportUnitTraps: 0,
        bottomPinnedWithoutSafeArea: 0,
    });
    assert.equal(line, "");
});

test("a result that never ran the focus-visible check is not flagged for it", () => {
    // The invalid-login probe persists only loginError and the console score.
    const line = summarize({ loginError: {}, consoleSeverityScore: 0, consoleSuppressed: 0 });
    assert.doesNotMatch(line, /noFocusVisible/);
});

test("a measured absence of :focus-visible rules is flagged", () => {
    assert.match(summarize({ hasFocusVisibleRules: false }), /noFocusVisible=true/);
    assert.doesNotMatch(summarize({ hasFocusVisibleRules: true }), /noFocusVisible/);
});

test("missing settings tabs are named in the summary", () => {
    const line = summarize({ settingsTabTitleGapMissing: ["settingsSystemTab", "settingsSecurityTab"] });
    assert.match(line, /settingsTabTitleGapMissing=settingsSystemTab\|settingsSecurityTab/);
});
