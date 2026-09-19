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
    loadAxeBuilder,
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
    // This exercises the analyze() catch; the load failure below covers the
    // other branch through the injected loader.
    const brokenPage = {};

    const result = await runAxeAudit(brokenPage);
    assert.equal(result.available, false);
    assert.match(result.error, /axe audit failed/, `unexpected error: ${result.error}`);
    assert.equal(result.violations, 0);
    // The caller distinguishes the two failure modes solely by `available`.
    assert.notEqual(result.available, true);
});

test("an uninstalled package reports available:false and names the fix", async () => {
    // The loader is injectable precisely so this path is reachable:
    // @axe-core/playwright resolves fine from tools/ui-lint in this repo, so
    // the real import cannot be made to fail here.
    const missing = () => Promise.reject(new Error("Cannot find package '@axe-core/playwright'"));

    const result = await runAxeAudit({}, { importAxe: missing });
    assert.equal(result.available, false);
    assert.equal(result.violations, 0);
    assert.equal(result.incomplete, 0);
    assert.deepEqual(result.critical, []);
    assert.match(result.error, /is not installed/);
    // The message has to say what to do about it, not just that it broke.
    assert.match(result.error, /npm run ui-lint:install/);
});

test("the two failure modes are told apart by their message, not by available", async () => {
    const loadFailure = await runAxeAudit({}, {
        importAxe: () => Promise.reject(new Error("boom")),
    });
    const runFailure = await runAxeAudit({});

    assert.equal(loadFailure.available, false);
    assert.equal(runFailure.available, false);
    assert.match(loadFailure.error, /is not installed/);
    assert.doesNotMatch(loadFailure.error, /axe audit failed/);
    assert.match(runFailure.error, /axe audit failed/);
    assert.doesNotMatch(runFailure.error, /is not installed/);
});

test("a module without an AxeBuilder export counts as uninstalled, not as a failed audit", async () => {
    // A half-installed package resolves but exports nothing usable. Calling
    // `new undefined()` would surface as "axe audit failed" and send whoever
    // reads the report looking for a page problem instead of an npm one.
    for (const broken of [{}, { default: undefined }, { default: {} }]) {
        const result = await runAxeAudit({}, { importAxe: () => Promise.resolve(broken) });
        assert.equal(result.available, false);
        assert.match(result.error, /is not installed/, JSON.stringify(broken));
    }
});

test("the loader hands back a usable AxeBuilder when the package is there", async () => {
    class StubBuilder { }
    const { AxeBuilder, error } = await loadAxeBuilder(
        () => Promise.resolve({ default: StubBuilder }),
    );

    assert.equal(AxeBuilder, StubBuilder);
    assert.equal(error, null);
});

test("the audited tag set is the one handed to withTags", async () => {
    // Guards the wiring between AXE_TAGS and the builder: a default argument
    // that stopped being passed through would silently narrow the ruleset
    // while every view still reported available:true.
    const seen = [];
    class RecordingBuilder {
        constructor(options) {
            seen.push({ page: options.page });
        }

        withTags(tags) {
            seen.push({ tags });
            return this;
        }

        analyze() {
            return Promise.resolve({ passes: [{}], violations: [], incomplete: [] });
        }
    }

    const page = { marker: "the page under audit" };
    const result = await runAxeAudit(page, {
        importAxe: () => Promise.resolve({ default: RecordingBuilder }),
    });

    assert.equal(result.available, true);
    assert.equal(result.passed, 1);
    assert.equal(seen[0].page, page);
    assert.deepEqual(seen[1].tags, [...AXE_TAGS]);

    // An explicit tag list must override the default rather than extend it.
    seen.length = 0;
    await runAxeAudit(page, {
        tags: ["wcag2aa"],
        importAxe: () => Promise.resolve({ default: RecordingBuilder }),
    });
    assert.deepEqual(seen[1].tags, ["wcag2aa"]);
});

// --- incomplete classification ------------------------------------------------
//
// The failure mode these guard against: an "incomplete" result that nobody has
// reviewed being swallowed along with the ones that were, which turns axe's
// request for a manual look into silence.
//

const {
    REVIEWED_INCOMPLETE_REASONS,
    axeIncompleteMessageKeys,
    normalizeAxeIncomplete,
    classifyAxeIncomplete,
} = await import("../../tools/ui-lint/lib/axe.mjs");

/** Builds an incomplete result carrying the given axe messageKeys. */
const incompleteWith = (id, messageKeys, bucket = "any") => ({
    id,
    nodes: [{ [bucket]: messageKeys.map((messageKey) => ({ id: id, data: { messageKey } })) }],
});

test("message keys are collected from every check bucket", () => {
    const keys = axeIncompleteMessageKeys({
        id: "color-contrast",
        nodes: [
            { any: [{ data: { messageKey: "bgGradient" } }] },
            { all: [{ data: { messageKey: "nonBmp" } }] },
            { none: [{ data: { messageKey: "bgImage" } }] },
        ],
    });
    assert.deepEqual(keys, ["bgGradient", "bgImage", "nonBmp"]);
});

test("repeated message keys collapse to one", () => {
    const keys = axeIncompleteMessageKeys({
        nodes: [
            { any: [{ data: { messageKey: "bgGradient" } }] },
            { any: [{ data: { messageKey: "bgGradient" } }] },
        ],
    });
    assert.deepEqual(keys, ["bgGradient"]);
});

test("the reviewed contrast limitations are recognized", () => {
    for (const messageKey of ["bgGradient", "nonBmp"]) {
        const entry = normalizeAxeIncomplete(incompleteWith("color-contrast", [messageKey]));
        assert.equal(entry.reviewed, true, `${messageKey} should be reviewed`);
    }
});

test("an unreviewed reason on a reviewed rule still needs review", () => {
    // Same rule, different reason: axe could not resolve a background *image*,
    // which is not the gradient case that was measured by hand.
    const entry = normalizeAxeIncomplete(incompleteWith("color-contrast", ["bgImage"]));
    assert.equal(entry.reviewed, false);
    assert.deepEqual(entry.messageKeys, ["bgImage"]);
});

test("a mix of reviewed and unreviewed reasons is not cleared", () => {
    const entry = normalizeAxeIncomplete(incompleteWith("color-contrast", ["bgGradient", "bgImage"]));
    assert.equal(entry.reviewed, false);
});

test("an incomplete result with no reason is never treated as reviewed", () => {
    // Absence of a messageKey is not a cleared messageKey: without a reason
    // there is nothing that could have been reviewed.
    const entry = normalizeAxeIncomplete({ id: "color-contrast", nodes: [{}] });
    assert.equal(entry.reviewed, false);
    assert.deepEqual(entry.messageKeys, []);
});

test("an unknown rule is never treated as reviewed", () => {
    const entry = normalizeAxeIncomplete(incompleteWith("aria-valid-attr-value", ["someReason"]));
    assert.equal(entry.reviewed, false);
    assert.equal(entry.id, "aria-valid-attr-value");
});

test("classification splits reviewed from unreviewed and keeps the total", () => {
    const classified = classifyAxeIncomplete([
        incompleteWith("color-contrast", ["bgGradient"]),
        incompleteWith("color-contrast", ["nonBmp"]),
        incompleteWith("color-contrast", ["bgImage"]),
        incompleteWith("region", ["somethingNew"]),
    ]);
    assert.equal(classified.total, 4);
    assert.equal(classified.reviewed.length, 2);
    assert.deepEqual(classified.unreviewed.map((e) => e.id), ["color-contrast", "region"]);
});

test("the summary reports the full incomplete count and the unreviewed subset", () => {
    const summary = summarizeAxeResults({
        violations: [],
        incomplete: [
            incompleteWith("color-contrast", ["bgGradient"]),
            incompleteWith("region", ["somethingNew"]),
        ],
    });
    // The metric must not shrink just because one case is acknowledged.
    assert.equal(summary.incomplete, 2);
    assert.equal(summary.incompleteReviewed, 1);
    assert.equal(summary.incompleteUnreviewed, 1);
    assert.deepEqual(summary.incompleteDetails.map((e) => e.id), ["region"]);
});

test("a fully reviewed page reports nothing left to review", () => {
    const summary = summarizeAxeResults({
        violations: [],
        incomplete: [incompleteWith("color-contrast", ["bgGradient"])],
    });
    assert.equal(summary.incomplete, 1);
    assert.equal(summary.incompleteUnreviewed, 0);
    assert.deepEqual(summary.incompleteDetails, []);
});

test("every reviewed reason is documented with a rule it belongs to", () => {
    // Guards against an entry being added as a bare rule id with an empty
    // reason list, which would clear every incomplete result for that rule.
    for (const [ruleId, reasons] of Object.entries(REVIEWED_INCOMPLETE_REASONS)) {
        assert.ok(ruleId.length > 0);
        assert.ok(Array.isArray(reasons) && reasons.length > 0, `${ruleId} lists no reasons`);
        for (const reason of reasons) {
            assert.equal(typeof reason, "string");
            assert.ok(reason.length > 0);
        }
    }
});
