//
// tools/ui-lint/lib/layout-shift.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Cumulative Layout Shift measurement.
//
// The runner already waits for the layout to settle before it measures or
// screenshots (waitForLayoutStability). That wait is what makes the audit
// deterministic, but it also hides the instability from the report: a view
// that reflows six times before it settles scores exactly like one that
// renders correctly on the first frame. This observer records the shifts that
// happen during that window, so the settling itself becomes a finding.
//
// Support is Chromium-only. WebKit and Firefox implement neither the
// `layout-shift` entry type nor the LayoutShift interface, so every touch
// profile reports `supported: false` rather than a fabricated 0 - a zero from
// an engine that cannot observe shifts would read as "this view is stable".
//

/** Where the page-side observer parks its state. */
const LAYOUT_SHIFT_KEY = '__uiLintLayoutShift';

/**
 * Registers the observer as an init script, so it is installed before any
 * document script runs and catches shifts from the very first frame.
 * Must be called on the BrowserContext, not the Page: a page-level script
 * would be installed after navigation has already begun.
 * @param {import('playwright').BrowserContext} context
 * @returns {Promise<void>}
 */
export async function installLayoutShiftObserver(context) {
    await context.addInitScript((stateKey) => {
        const supported = typeof PerformanceObserver !== 'undefined'
            && Array.isArray(PerformanceObserver.supportedEntryTypes)
            && PerformanceObserver.supportedEntryTypes.includes('layout-shift');

        const state = { value: 0, count: 0, largest: 0, entries: [], supported };
        window[stateKey] = state;

        if (!supported) return;

        try {
            const observer = new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    // Shifts within 500ms of a user interaction are expected
                    // (a modal opening, a tab switching) and excluded from CLS
                    // by the spec.
                    if (entry.hadRecentInput) continue;

                    const value = entry.value || 0;
                    state.value += value;
                    state.count += 1;
                    if (value > state.largest) state.largest = value;

                    // Capped: a thrashing page can emit thousands of entries,
                    // and only the worst few are actionable.
                    if (state.entries.length < 20) {
                        state.entries.push({
                            value,
                            startTime: entry.startTime || 0,
                            sources: (entry.sources || []).slice(0, 3).map((source) => ({
                                node: source.node?.tagName
                                    ? `${source.node.tagName.toLowerCase()}${source.node.id ? `#${source.node.id}` : ''}`
                                    : null,
                            })),
                        });
                    }
                }
            });
            observer.observe({ type: 'layout-shift', buffered: true });
        } catch {
            // An engine that advertises the entry type but rejects the
            // observe() call leaves `supported` true and the totals at zero;
            // treat it as unsupported instead.
            state.supported = false;
        }
    }, LAYOUT_SHIFT_KEY);
}

/**
 * Reads the accumulated shift state out of the page.
 * @param {import('playwright').Page} page
 * @returns {Promise<{value: number, count: number, largest: number, entries: object[], supported: boolean}>}
 */
export async function collectLayoutShift(page) {
    return page
        .evaluate(
            (stateKey) => window[stateKey] || { value: 0, count: 0, largest: 0, entries: [], supported: false },
            LAYOUT_SHIFT_KEY,
        )
        .catch(() => ({ value: 0, count: 0, largest: 0, entries: [], supported: false }));
}

// Google's Core Web Vitals boundaries. "good" below 0.1, "poor" above 0.25.
export const LAYOUT_SHIFT_GOOD = 0.1;
export const LAYOUT_SHIFT_POOR = 0.25;

/**
 * Classifies a CLS value. Returns 'unsupported' when the engine could not
 * observe, so callers never present a missing measurement as a passing one.
 * @param {{value?: number, supported?: boolean}} layoutShift
 * @returns {'unsupported'|'good'|'needs-improvement'|'poor'}
 */
export function classifyLayoutShift(layoutShift = {}) {
    if (!layoutShift.supported) return 'unsupported';
    const value = Number(layoutShift.value || 0);
    if (value <= LAYOUT_SHIFT_GOOD) return 'good';
    if (value <= LAYOUT_SHIFT_POOR) return 'needs-improvement';
    return 'poor';
}
