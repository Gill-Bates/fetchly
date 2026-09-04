//
// tests/js/ui-lint-axe.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The failure mode this file guards against: an accessibility audit that did
// not run must never be reportable as an audit that found nothing.
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    AXE_TAGS,
    normalizeAxeViolation,
    summarizeAxeResults,
    runAxeAudit,
} = await import("../../tools/ui-lint/lib/axe.mjs");

test("runs the WCAG 2.1 A and AA rulesets", () => {
    for (const tag of ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]) {
        assert.ok(AXE_TAGS.includes(tag), `${tag} is not audited`);
    }
});

test("a violation keeps its real node count while capping the node list", () => {
    const nodes = Array.from({ length: 40 }, (_, index) => ({
        target: [`#control-${index}`],
        html: "x".repeat(500),
        failureSummary: "Fix this",
    }));

    const normalized = normalizeAxeViolation({
        id: "button-name",
        impact: "critical",
        help: "Buttons must have discernible text",
        helpUrl: "https://example.invalid/button-name",
        nodes,
    });

    assert.equal(normalized.nodeCount, 40);
    assert.equal(normalized.nodes.length, 5);
    assert.ok(normalized.nodes[0].html.length <= 200);
});

test("violations are bucketed by impact", () => {
    const summary = summarizeAxeResults({
        passes: [{}, {}],
        incomplete: [{}],
        violations: [
            { id: "button-name", impact: "critical", nodes: [{}] },
            { id: "color-contrast", impact: "serious", nodes: [{}, {}] },
            { id: "region", impact: "moderate", nodes: [] },
        ],
    });

    assert.equal(summary.available, true);
    assert.equal(summary.violations, 3);
    assert.equal(summary.passed, 2);
    assert.equal(summary.incomplete, 1);
    assert.deepEqual(summary.critical.map((v) => v.id), ["button-name"]);
    assert.deepEqual(summary.serious.map((v) => v.id), ["color-contrast"]);
    assert.deepEqual(summary.moderate.map((v) => v.id), ["region"]);
    assert.deepEqual(summary.minor, []);
});

test("a violation with no impact is kept, not dropped", () => {
    const summary = summarizeAxeResults({ violations: [{ id: "odd", nodes: [] }] });
    assert.equal(summary.violations, 1);
    assert.equal(summary.minor.length, 1);
});

test("a failed run reports available:false with a reason, not an empty pass", () => {
    // Passing a page object whose evaluate throws stands in for any runtime
    // failure inside axe.
    const brokenPage = {};

    return runAxeAudit(brokenPage).then((result) => {
        assert.equal(result.available, false);
        assert.ok(result.error, "no reason given for the skipped audit");
        assert.equal(result.violations, 0);
        // The caller distinguishes these two cases solely by `available`.
        assert.notEqual(result.available, true);
    });
});
