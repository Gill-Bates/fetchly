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

test("a runtime failure inside axe reports available:false, not an empty pass", async () => {
    // An empty page object stands in for any runtime failure AxeBuilder can
    // hit against a real page (navigation gone, context closed, ...): it has
    // none of the methods AxeBuilder needs and analyze() rejects.
    //
    // This exercises the second catch in runAxeAudit, not the "package not
    // installed" branch: @axe-core/playwright is a devDependency of
    // tools/ui-lint and resolves from there in this repo, so a test cannot
    // force the import itself to fail without mocking module resolution.
    const brokenPage = {};

    const result = await runAxeAudit(brokenPage);
    assert.equal(result.available, false);
    assert.match(result.error, /axe audit failed/, `unexpected error: ${result.error}`);
    assert.equal(result.violations, 0);
    // The caller distinguishes the two failure modes solely by `available`.
    assert.notEqual(result.available, true);
});
