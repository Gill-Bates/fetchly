//
// tests/js/ui-lint-health.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The health score exists so that clearing warnings shows up as progress. It
// is only useful if the ordering is stable: a broken page must never outscore
// a merely untidy one, and a missing measurement must never earn points.
//

import assert from "node:assert/strict";
import test from "node:test";

const {
    buildUIHealthReport,
    classifyUxIssue,
    deriveUxIssues,
    scoreUxIssues,
    summarizeHealthReports,
} = await import("../../tools/ui-lint/lib/ui-health.mjs");

const clean = () => buildUIHealthReport({
    name: "dashboard",
    metrics: {},
    console: { score: 0 },
    axe: { available: true, critical: [], serious: [], moderate: [], minor: [], passed: 30 },
    layoutShift: { value: 0, count: 0, supported: true },
    visualDriftRatio: 0,
});

test("a clean view scores 100 and is healthy", () => {
    const report = clean();
    assert.equal(report.score, 100);
    assert.equal(report.severity, "healthy");
    assert.equal(report.gates.hardBlock, false);
});

test("an explicit severity wins over the kind lookup", () => {
    assert.equal(classifyUxIssue({ kind: "token-drift", severity: "critical" }), "critical");
    assert.equal(classifyUxIssue({ kind: "horizontal-overflow" }), "critical");
});

test("an unknown kind is scored as minor rather than dropped", () => {
    assert.equal(classifyUxIssue({ kind: "something-new" }), "minor");
    assert.equal(scoreUxIssues([{ kind: "something-new" }]).total, 1);
});

test("overflow outweighs token drift", () => {
    const overflow = scoreUxIssues(deriveUxIssues({
        metrics: { horizontalOverflow: true, overflowAmount: 40 },
    }));
    const drift = scoreUxIssues(deriveUxIssues({
        metrics: { hardcodedColors: new Array(60), tokenViolations: new Array(10) },
    }));

    assert.ok(overflow.score > drift.score, "a broken layout scores no worse than untidy colours");
    assert.equal(overflow.critical.length, 1);
    assert.equal(drift.critical.length, 0);
});

test("no single dimension can drive the score to zero on its own", () => {
    // 200 hardcoded colours is debt, not a broken page. It must not score the
    // same as a view that fails to render.
    const untidy = buildUIHealthReport({
        metrics: { hardcodedColors: new Array(200), spacingIssues: new Array(50) },
        console: { score: 0 },
        axe: { available: true, critical: [], serious: [], moderate: [], minor: [] },
        layoutShift: { supported: true, value: 0 },
    });

    assert.ok(untidy.score > 0);
    assert.equal(untidy.gates.hardBlock, false);
});

test("a critical UX issue hard-blocks regardless of the score", () => {
    const report = buildUIHealthReport({
        metrics: { smallTouchTargets: [{}] },
        console: { score: 0 },
        axe: { available: true, critical: [], serious: [], moderate: [], minor: [] },
        layoutShift: { supported: true, value: 0 },
    });

    assert.equal(report.gates.hardBlock, true);
    assert.ok(report.score > 70, "a single touch target should not tank the score, only gate it");
});

test("a critical axe violation hard-blocks", () => {
    const report = buildUIHealthReport({
        metrics: {},
        console: { score: 0 },
        axe: { available: true, critical: [{ id: "button-name" }], serious: [], moderate: [], minor: [] },
        layoutShift: { supported: true, value: 0 },
    });

    assert.equal(report.gates.hardBlock, true);
});

test("an axe audit that did not run is neither a penalty nor a pass", () => {
    const skipped = buildUIHealthReport({
        metrics: {},
        console: { score: 0 },
        axe: { available: false, error: "package missing" },
        layoutShift: { supported: true, value: 0 },
    });

    assert.equal(skipped.accessibility.available, false);
    assert.ok(skipped.accessibility.reason);
    assert.equal(skipped.penalties.accessibility, 0);
    // Not hard-blocked either: the audit has no opinion, so it must not veto.
    assert.equal(skipped.gates.hardBlock, false);
});

test("an unsupported engine is not penalised for layout shift", () => {
    const webkit = buildUIHealthReport({
        metrics: {},
        console: { score: 0 },
        axe: { available: true, critical: [], serious: [], moderate: [], minor: [] },
        layoutShift: { supported: false, value: 0 },
    });

    assert.equal(webkit.penalties.layoutShift, 0);
    assert.equal(webkit.layoutShift.rating, "unsupported");
    assert.equal(webkit.score, 100);
});

test("a poor CLS costs points and lands as a serious issue", () => {
    const shifty = buildUIHealthReport({
        metrics: {},
        console: { score: 0 },
        axe: { available: true, critical: [], serious: [], moderate: [], minor: [] },
        layoutShift: { supported: true, value: 0.4, count: 12 },
    });

    assert.ok(shifty.penalties.layoutShift > 0);
    assert.equal(shifty.ux.serious, 1);
    assert.ok(shifty.score < clean().score);
});

test("the run summary reports the worst view, not the average alone", () => {
    const summary = summarizeHealthReports([
        { name: "a", score: 100, severity: "healthy", gates: { hardBlock: false } },
        { name: "b", score: 40, severity: "critical", gates: { hardBlock: true } },
    ]);

    assert.equal(summary.worstScore, 40);
    assert.equal(summary.averageScore, 70);
    assert.equal(summary.critical, 1);
    assert.deepEqual(summary.hardBlocked, ["b"]);
});

test("an empty run summarizes without throwing", () => {
    const summary = summarizeHealthReports([]);
    assert.equal(summary.views, 0);
    assert.equal(summary.worstScore, null);
});
