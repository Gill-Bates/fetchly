//
// tools/ui-lint/lib/axe.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// axe-core accessibility audit.
//
// The runner's own accessibility checks are hand-written and narrow: they know
// about icon buttons without aria-label, unlabeled inputs, duplicate ids and a
// luminance-based contrast ratio. That covers a handful of the ~90 WCAG 2.1
// A/AA rules axe implements. This module adds the rest instead of growing the
// hand-written set one regression at a time.
//
// The context the runner creates sets bypassCSP, which axe needs: it injects
// its ruleset into the page, and app/main.py serves a script-src 'self' policy
// that would otherwise block it.
//

/** axe rule tags the audit runs. Kept in one place so every view agrees. */
export const AXE_TAGS = Object.freeze([
    'wcag2a',
    'wcag2aa',
    'wcag21a',
    'wcag21aa',
    'best-practice',
]);

/** axe impact levels, most severe first. */
export const AXE_IMPACTS = Object.freeze(['critical', 'serious', 'moderate', 'minor']);

/**
 * axe "incomplete" results that have been reviewed by hand and found to be
 * limits of the checker rather than defects in the page.
 *
 * axe reports "incomplete" when a rule cannot reach a verdict on its own. That
 * is a request for a human to look, not a finding - so re-reporting the same
 * known-unresolvable cases on every view teaches readers to ignore the warning,
 * and the real one then goes unnoticed too. Each entry below records a case that
 * was checked manually, so anything outside this list still surfaces.
 *
 * Keyed by axe rule id, then by the `messageKey` axe puts in the check data.
 *
 * - color-contrast / bgGradient
 *   The card surfaces are layered: `linear-gradient(rgba(255,255,255,0.02), …)`
 *   over a `radial-gradient(… rgba(56,189,248,0.14) …)` over `#0f172a`. axe
 *   refuses to guess a single background colour behind a gradient and stops.
 *   Compositing those layers by hand gives #0f172a under the affected labels
 *   and 17.06:1 against their `rgb(248,250,252)` text, where AA asks 4.5:1.
 *   Even at the gradient's strongest point the tint only reaches rgb(21,46,71),
 *   which still clears 13:1 - there is no viewport position where these drop to
 *   4.5:1.
 *
 * - color-contrast / nonBmp
 *   The element's text is a single symbolic glyph with no contrast requirement
 *   of its own: the `∞` in the retention slider's `aria-hidden="true"` scale
 *   labels. Decorative, hidden from assistive technology, and measured at
 *   12.56:1 regardless.
 *
 * - color-contrast / shortTextContent
 *   Text too short for axe to sample a background from: the `·` separators in
 *   the footer link row. Measured by hand at 12.76:1, `rgb(208,217,232)` on
 *   `rgb(14,21,39)`.
 *
 * Deliberately *not* listed: `elmPartiallyObscured`. It appears only on the two
 * desktop dashboards and only intermittently - it could not be reproduced by
 * replaying the audit's Job History toggle sequence, so whatever overlapped the
 * text was transient. An overlap that hides text is a real defect, and nothing
 * here establishes that this one is not, so it keeps warning until someone
 * catches it in the act.
 */
export const REVIEWED_INCOMPLETE_REASONS = Object.freeze({
    'color-contrast': Object.freeze(['bgGradient', 'nonBmp', 'shortTextContent']),
});

/**
 * Collects the distinct `messageKey` values axe attached to one incomplete
 * result. The key says *why* the rule could not decide, which is what makes a
 * case reviewable at all; the rule id alone does not.
 * @param {object} incomplete one entry of axeResults.incomplete
 * @returns {string[]} sorted, de-duplicated message keys
 */
export function axeIncompleteMessageKeys(incomplete) {
    const keys = new Set();
    for (const node of incomplete?.nodes || []) {
        for (const check of [...(node?.any || []), ...(node?.all || []), ...(node?.none || [])]) {
            const key = check?.data?.messageKey;
            if (typeof key === 'string' && key) keys.add(key);
        }
    }
    return [...keys].sort();
}

/**
 * Reduces one incomplete result to its identity plus whether every reason it
 * carries has already been reviewed.
 *
 * A result counts as reviewed only when it names at least one message key and
 * *all* of its keys are listed for that rule. An incomplete result with no
 * message key is never treated as reviewed: absence of a reason is not a
 * cleared reason.
 * @param {object} incomplete
 * @returns {{id: string, messageKeys: string[], nodeCount: number, reviewed: boolean}}
 */
export function normalizeAxeIncomplete(incomplete) {
    const id = incomplete?.id || 'unknown';
    const messageKeys = axeIncompleteMessageKeys(incomplete);
    const acknowledged = REVIEWED_INCOMPLETE_REASONS[id] || [];
    const nodes = incomplete?.nodes || [];

    // "Needs manual review" is not actionable without saying which element and
    // for which reason, so the unreviewed reasons are carried with their nodes.
    // Only nodes whose reason is still open are kept: on a page where one rule
    // reports a dozen accepted cases and one new one, listing all of them again
    // buries the new one.
    const unreviewedNodes = [];
    for (const node of nodes) {
        const checks = [...(node?.any || []), ...(node?.all || []), ...(node?.none || [])];
        const keys = [...new Set(
            checks.map((check) => check?.data?.messageKey).filter((key) => typeof key === 'string' && key),
        )];
        const open = keys.filter((key) => !acknowledged.includes(key));
        if (keys.length > 0 && open.length === 0) continue;
        unreviewedNodes.push({
            target: node?.target || [],
            html: typeof node?.html === 'string' ? node.html.slice(0, 160) : null,
            messageKeys: open.length > 0 ? open.sort() : keys,
        });
    }

    return {
        id,
        messageKeys,
        nodeCount: nodes.length,
        reviewed: messageKeys.length > 0
            && messageKeys.every((key) => acknowledged.includes(key))
            && unreviewedNodes.length === 0,
        // Capped for the same reason normalizeAxeViolation caps its node list.
        nodes: unreviewedNodes.slice(0, 5),
        unreviewedNodeCount: unreviewedNodes.length,
    };
}

/**
 * Splits incomplete results into the reviewed ones and the rest.
 * @param {object[]} incomplete axeResults.incomplete
 * @returns {{total: number, reviewed: object[], unreviewed: object[]}}
 */
export function classifyAxeIncomplete(incomplete) {
    const normalized = (incomplete || []).map(normalizeAxeIncomplete);
    return {
        total: normalized.length,
        reviewed: normalized.filter((entry) => entry.reviewed),
        unreviewed: normalized.filter((entry) => !entry.reviewed),
    };
}

/** Empty result, so callers never have to null-check the audit away. */
function emptyAxeResult(extra = {}) {
    return {
        available: false,
        passed: 0,
        violations: 0,
        incomplete: 0,
        incompleteUnreviewed: 0,
        incompleteDetails: [],
        incompleteReviewed: 0,
        critical: [],
        serious: [],
        moderate: [],
        minor: [],
        ...extra,
    };
}

/**
 * Reduces one axe violation to the fields worth keeping in results.json.
 * The full node list on a broken page can run to hundreds of entries; the cap
 * keeps the report readable while `nodeCount` preserves the real number.
 * @param {object} violation
 * @returns {object}
 */
export function normalizeAxeViolation(violation) {
    const nodes = violation?.nodes || [];
    return {
        id: violation?.id || 'unknown',
        impact: violation?.impact || 'minor',
        help: violation?.help || violation?.description || 'Accessibility violation',
        helpUrl: violation?.helpUrl || null,
        tags: violation?.tags || [],
        nodeCount: nodes.length,
        nodes: nodes.slice(0, 5).map((node) => ({
            target: node?.target || [],
            html: typeof node?.html === 'string' ? node.html.slice(0, 200) : null,
            failureSummary: node?.failureSummary || null,
        })),
    };
}

/**
 * Groups normalized violations into the impact buckets and counts.
 * Split from runAxeAudit so it can be tested without a browser.
 * @param {object} axeResults raw output of AxeBuilder.analyze()
 * @returns {object}
 */
export function summarizeAxeResults(axeResults) {
    const violations = (axeResults?.violations || []).map(normalizeAxeViolation);
    const byImpact = Object.fromEntries(
        AXE_IMPACTS.map((impact) => [impact, violations.filter((v) => v.impact === impact)]),
    );
    const incomplete = classifyAxeIncomplete(axeResults?.incomplete);

    return {
        available: true,
        passed: axeResults?.passes?.length || 0,
        violations: violations.length,
        // The full count stays the reported metric, so suppressing a warning
        // never hides that axe left checks undecided.
        incomplete: incomplete.total,
        // What a human still has to look at. The warning is keyed off this.
        incompleteUnreviewed: incomplete.unreviewed.length,
        incompleteDetails: incomplete.unreviewed,
        incompleteReviewed: incomplete.reviewed.length,
        ...byImpact,
    };
}

/** The real module load, kept separate so tests can substitute a failing one. */
const importAxeModule = () => import('@axe-core/playwright');

/**
 * Resolves AxeBuilder, or the reason it is unusable.
 *
 * Split out and parameterized because this is one of the two distinct failure
 * paths in runAxeAudit, and the only one that cannot be provoked from a test
 * otherwise: @axe-core/playwright is a devDependency of tools/ui-lint and
 * resolves fine in this repo, so the import never fails on its own.
 *
 * A module that resolves but exports no callable default is treated as
 * missing: calling `new undefined()` would land in the analyze() catch and
 * mislabel a broken install as a failed audit.
 *
 * @param {() => Promise<{default: unknown}>} [importAxe]
 * @returns {Promise<{AxeBuilder: Function | null, error: string | null}>}
 */
export async function loadAxeBuilder(importAxe = importAxeModule) {
    try {
        const { default: AxeBuilder } = await importAxe();
        if (typeof AxeBuilder !== 'function') {
            return {
                AxeBuilder: null,
                error: '@axe-core/playwright is not installed (no AxeBuilder export); run npm run ui-lint:install',
            };
        }
        return { AxeBuilder, error: null };
    } catch (error) {
        return {
            AxeBuilder: null,
            error: `@axe-core/playwright is not installed (${error.message}); run npm run ui-lint:install`,
        };
    }
}

/**
 * Runs axe against the current page state.
 *
 * A missing @axe-core/playwright package is reported as `available: false`
 * with the reason rather than throwing: the audit still produces every other
 * finding, and the console summary says the a11y pass did not run. Any other
 * failure is surfaced the same way, because an accessibility audit that
 * silently returns "no violations" is worse than none.
 *
 * @param {import('playwright').Page} page
 * @param {{tags?: string[], importAxe?: () => Promise<{default: unknown}>}} [options]
 * @returns {Promise<object>}
 */
export async function runAxeAudit(page, { tags = AXE_TAGS, importAxe = importAxeModule } = {}) {
    const { AxeBuilder, error: loadError } = await loadAxeBuilder(importAxe);
    if (!AxeBuilder) {
        return emptyAxeResult({ error: loadError });
    }

    try {
        const axeResults = await new AxeBuilder({ page }).withTags(tags).analyze();
        return summarizeAxeResults(axeResults);
    } catch (error) {
        return emptyAxeResult({ error: `axe audit failed: ${error.message}` });
    }
}
