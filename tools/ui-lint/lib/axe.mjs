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

/** Empty result, so callers never have to null-check the audit away. */
function emptyAxeResult(extra = {}) {
    return {
        available: false,
        passed: 0,
        violations: 0,
        incomplete: 0,
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

    return {
        available: true,
        passed: axeResults?.passes?.length || 0,
        violations: violations.length,
        incomplete: axeResults?.incomplete?.length || 0,
        ...byImpact,
    };
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
 * @param {{tags?: string[]}} [options]
 * @returns {Promise<object>}
 */
export async function runAxeAudit(page, { tags = AXE_TAGS } = {}) {
    let AxeBuilder;
    try {
        ({ default: AxeBuilder } = await import('@axe-core/playwright'));
    } catch (error) {
        return emptyAxeResult({
            error: `@axe-core/playwright is not installed (${error.message}); run npm run ui-lint:install`,
        });
    }

    try {
        const axeResults = await new AxeBuilder({ page }).withTags(tags).analyze();
        return summarizeAxeResults(axeResults);
    } catch (error) {
        return emptyAxeResult({ error: `axe audit failed: ${error.message}` });
    }
}
