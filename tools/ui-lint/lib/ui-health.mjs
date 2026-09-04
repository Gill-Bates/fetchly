//
// tools/ui-lint/lib/ui-health.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Weighted UI health score.
//
// The runner's own verdict is binary: any entry in `failures` fails the view,
// everything else is a warning nobody reads. That makes every failure look
// alike - a horizontal overflow that breaks the page and one hardcoded colour
// over the threshold both print FAIL - and it makes progress invisible, since
// clearing 40 warnings changes nothing an exit code can show.
//
// This module keeps the binary gate intact and adds a graded view on top:
// findings are classified into critical/serious/minor, converted into
// penalties, and subtracted from 100. The score is reported for every view and
// can optionally gate the run via UI_LINT_HEALTH_MIN, which defaults to off.
//

import { classifyLayoutShift } from './layout-shift.mjs';

/** Findings that make the view unusable for someone. */
const CRITICAL_ISSUE_KINDS = new Set([
    'horizontal-overflow',
    'touch-target',
    'invisible-interactive',
    'missing-selector',
    'ios-input-zoom',
    'accessibility-blocking',
]);

/** Findings that degrade the view without breaking it outright. */
const SERIOUS_ISSUE_KINDS = new Set([
    'clipped-action',
    'scroll-trap',
    'layout-shift',
    'entity-overlap',
    'accessibility-regression',
    'broken-media',
    'visual-instability',
]);

/** Findings that are consistency debt rather than defects. */
const MINOR_ISSUE_KINDS = new Set([
    'spacing-inconsistency',
    'token-drift',
    'weak-contrast',
]);

const SEVERITY_POINTS = Object.freeze({ critical: 8, serious: 4, minor: 1 });

/** Array length, or the number itself when a caller already flattened it. */
function count(value) {
    if (Array.isArray(value)) return value.length;
    if (typeof value === 'boolean') return value ? 1 : 0;
    return Number(value || 0);
}

/**
 * Resolves an issue's severity: an explicit one wins, otherwise the kind
 * decides, and an unknown kind falls back to minor rather than being dropped.
 * @param {{kind?: string, severity?: string}} issue
 * @returns {'critical'|'serious'|'minor'}
 */
export function classifyUxIssue(issue = {}) {
    if (['critical', 'serious', 'minor'].includes(issue.severity)) {
        return issue.severity;
    }

    const kind = String(issue.kind || '').trim().toLowerCase().replace(/_/g, '-');
    if (CRITICAL_ISSUE_KINDS.has(kind)) return 'critical';
    if (SERIOUS_ISSUE_KINDS.has(kind)) return 'serious';
    if (MINOR_ISSUE_KINDS.has(kind)) return 'minor';
    return 'minor';
}

/**
 * Buckets issues and returns their weighted total.
 * @param {object[]} issues
 * @returns {{score: number, total: number, critical: object[], serious: object[], minor: object[]}}
 */
export function scoreUxIssues(issues = []) {
    const critical = [];
    const serious = [];
    const minor = [];
    let score = 0;

    for (const issue of issues) {
        const severity = classifyUxIssue(issue);
        const entry = { ...issue, severity };
        score += SEVERITY_POINTS[severity];

        if (severity === 'critical') critical.push(entry);
        else if (severity === 'serious') serious.push(entry);
        else minor.push(entry);
    }

    return { score, total: issues.length, critical, serious, minor };
}

/**
 * Translates the runner's raw metrics object into severity-carrying issues.
 *
 * Only metrics that describe something a user can perceive are listed here.
 * Source-contract metrics (jobsPagingOffsetContractBroken and friends) stay
 * out on purpose: they are real regressions, but they are already hard
 * failures and scoring them would double-count the same defect.
 *
 * @param {{metrics?: object, visualDriftRatio?: number, layoutShift?: object}} input
 * @returns {object[]}
 */
export function deriveUxIssues({ metrics = {}, visualDriftRatio = 0, layoutShift = {} } = {}) {
    const issues = [];
    const add = (kind, severity, text, value) => issues.push({ kind, severity, text, value });

    if (metrics.horizontalOverflow) {
        add('horizontal-overflow', 'critical', `page scrolls horizontally by ${count(metrics.overflowAmount)}px`, count(metrics.overflowAmount));
    }
    if (count(metrics.missingSelectors)) {
        add('missing-selector', 'critical', 'required elements did not render', count(metrics.missingSelectors));
    }
    if (count(metrics.smallTouchTargets)) {
        add('touch-target', 'critical', 'controls below the minimum hit area', count(metrics.smallTouchTargets));
    }
    if (count(metrics.iosInputZoomTargets)) {
        add('ios-input-zoom', 'critical', 'controls that zoom the page on iOS focus', count(metrics.iosInputZoomTargets));
    }
    if (count(metrics.iconButtonsWithoutAria) || count(metrics.unlabeledInputsStrict)) {
        add('accessibility-blocking', 'critical', 'interactive elements without an accessible name',
            count(metrics.iconButtonsWithoutAria) + count(metrics.unlabeledInputsStrict));
    }

    if (count(metrics.clippedDropdowns)) {
        add('clipped-action', 'serious', 'dropdowns clipped by an ancestor', count(metrics.clippedDropdowns));
    }
    if (count(metrics.tightlyPackedTargets)) {
        add('clipped-action', 'serious', 'controls packed below the minimum spacing', count(metrics.tightlyPackedTargets));
    }
    const scrollTraps = count(metrics.nestedScrollContainers)
        + count(metrics.flexScrollTraps)
        + count(metrics.ghostScrollContainers)
        + count(metrics.overflowHiddenScrollBlockers)
        + count(metrics.doubleScrollRisk)
        + count(metrics.viewportScrollLeak);
    if (scrollTraps) {
        add('scroll-trap', 'serious', 'scroll containers that trap or leak the gesture', scrollTraps);
    }
    if (count(metrics.overlapIssues) || count(metrics.localOverflowIssues)) {
        add('entity-overlap', 'serious', 'elements overlapping or overflowing their container',
            count(metrics.overlapIssues) + count(metrics.localOverflowIssues));
    }
    if (count(metrics.focusIndicatorMissing) || count(metrics.duplicateIds) || count(metrics.unlabeledControls)) {
        add('accessibility-regression', 'serious', 'focus, id or labelling contract broken',
            count(metrics.focusIndicatorMissing) + count(metrics.duplicateIds) + count(metrics.unlabeledControls));
    }
    if (count(metrics.brokenImages) || count(metrics.brokenIcons) || count(metrics.invisibleMedia)) {
        add('broken-media', 'serious', 'images, icons or media that did not render',
            count(metrics.brokenImages) + count(metrics.brokenIcons) + count(metrics.invisibleMedia));
    }
    // Only an engine that can observe shifts gets to report them; on WebKit
    // and Firefox `supported` is false and the view is neither credited nor
    // penalised for its stability.
    if (classifyLayoutShift(layoutShift) === 'poor') {
        add('layout-shift', 'serious', `cumulative layout shift ${Number(layoutShift.value).toFixed(3)}`, layoutShift.value);
    }

    if (count(metrics.tokenViolations) || count(metrics.hardcodedColors)) {
        add('token-drift', 'minor', 'colours and sizes bypassing the design tokens',
            count(metrics.tokenViolations) + count(metrics.hardcodedColors));
    }
    if (count(metrics.contrastIssues)) {
        add('weak-contrast', 'minor', 'text below the contrast target', count(metrics.contrastIssues));
    }
    if (count(metrics.spacingIssues) || count(metrics.alignmentIssues) || count(metrics.badgeInconsistencies)) {
        add('spacing-inconsistency', 'minor', 'spacing, alignment or badge inconsistencies',
            count(metrics.spacingIssues) + count(metrics.alignmentIssues) + count(metrics.badgeInconsistencies));
    }
    if (Number(visualDriftRatio) > 0) {
        add('visual-instability', Number(visualDriftRatio) > 0.05 ? 'serious' : 'minor',
            `screenshot pair differs by ${(Number(visualDriftRatio) * 100).toFixed(2)}%`, visualDriftRatio);
    }

    return issues;
}

/**
 * Builds the health report for a single view.
 *
 * Penalties are capped individually so no single dimension can drive the score
 * to zero on its own - a page with 200 hardcoded colours and nothing else
 * wrong should not score the same as one that does not render.
 *
 * @param {object} input
 * @param {string} [input.name] view name
 * @param {string} [input.url]
 * @param {string} [input.device] device profile name
 * @param {string} [input.engine] browser engine
 * @param {object} [input.metrics] raw metrics from collectMetrics()
 * @param {object} [input.console] output of triageConsoleEntries()
 * @param {object} [input.axe] output of runAxeAudit()
 * @param {object} [input.layoutShift] output of collectLayoutShift()
 * @param {number} [input.visualDriftRatio]
 * @returns {object}
 */
export function buildUIHealthReport({
    name = null,
    url = null,
    device = null,
    engine = null,
    metrics = {},
    console: consoleTriage = {},
    axe = {},
    layoutShift = {},
    visualDriftRatio = 0,
} = {}) {
    const issues = deriveUxIssues({ metrics, visualDriftRatio, layoutShift });
    const ux = scoreUxIssues(issues);

    const consolePenalty = Math.min(20, (consoleTriage.score || 0) * 2);
    const uxPenalty = Math.min(45, ux.score);
    const accessibilityPenalty = axe.available
        ? Math.min(30, count(axe.critical) * 12 + count(axe.serious) * 8 + count(axe.moderate) * 4 + count(axe.minor) * 2)
        : 0;
    const layoutShiftClass = classifyLayoutShift(layoutShift);
    const layoutPenalty = layoutShift.supported
        ? Math.min(15, Math.round(Number(layoutShift.value || 0) * 100))
        : 0;
    const visualPenalty = Math.min(15, Math.round(Number(visualDriftRatio || 0) * 100));

    const penalties = {
        console: consolePenalty,
        ux: uxPenalty,
        accessibility: accessibilityPenalty,
        layoutShift: layoutPenalty,
        visual: visualPenalty,
    };

    const total = Object.values(penalties).reduce((sum, value) => sum + value, 0);
    const score = Math.max(0, 100 - total);
    const severity = score >= 85 ? 'healthy' : score >= 70 ? 'degraded' : 'critical';

    return {
        name,
        url,
        device,
        engine,
        score,
        severity,
        penalties,
        ux: {
            score: ux.score,
            critical: ux.critical.length,
            serious: ux.serious.length,
            minor: ux.minor.length,
            issues,
        },
        console: {
            score: consoleTriage.score || 0,
            critical: count(consoleTriage.critical),
            serious: count(consoleTriage.serious),
            suppressed: consoleTriage.suppressed || 0,
        },
        accessibility: axe.available
            ? {
                available: true,
                critical: count(axe.critical),
                serious: count(axe.serious),
                moderate: count(axe.moderate),
                minor: count(axe.minor),
                passed: axe.passed || 0,
            }
            : { available: false, reason: axe.error || 'axe audit did not run' },
        layoutShift: {
            supported: Boolean(layoutShift.supported),
            value: Number(Number(layoutShift.value || 0).toFixed(4)),
            count: Number(layoutShift.count || 0),
            rating: layoutShiftClass,
        },
        // The hard gate stays separate from the score: a view can sit at 78
        // and still be shippable, but a critical UX issue or a blocking axe
        // violation is never negotiable.
        gates: {
            hardBlock: ux.critical.length > 0 || (axe.available && count(axe.critical) > 0),
        },
    };
}

/**
 * Rolls per-view reports into one run-level summary.
 * @param {object[]} reports
 * @returns {object}
 */
export function summarizeHealthReports(reports = []) {
    if (!reports.length) {
        return { views: 0, worstScore: null, averageScore: null, healthy: 0, degraded: 0, critical: 0, hardBlocked: [] };
    }

    const scores = reports.map((report) => report.score);
    return {
        views: reports.length,
        worstScore: Math.min(...scores),
        averageScore: Math.round(scores.reduce((sum, value) => sum + value, 0) / scores.length),
        healthy: reports.filter((report) => report.severity === 'healthy').length,
        degraded: reports.filter((report) => report.severity === 'degraded').length,
        critical: reports.filter((report) => report.severity === 'critical').length,
        hardBlocked: reports.filter((report) => report.gates?.hardBlock).map((report) => report.name),
    };
}
