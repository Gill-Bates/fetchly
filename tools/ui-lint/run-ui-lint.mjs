//
// tools/ui-lint/run-ui-lint.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { writeFile } from 'node:fs/promises';
import path from 'node:path';

import { chromium, devices } from 'playwright';

import {
    collectConsoleAndNetwork,
    diffScreenshots,
    disableMotion,
    ensureDir,
    login,
    sanitize,
} from './lib/browser-utils.mjs';

const BASE_URL = process.env.UI_LINT_BASE_URL || 'http://127.0.0.1:8000';
const BASE_ORIGIN = new URL(BASE_URL).origin;
const USERNAME = process.env.UI_LINT_USERNAME;
const PASSWORD = process.env.UI_LINT_PASSWORD;
const SESSION_ID = Date.now();
const OUTPUT_DIR = process.env.UI_LINT_OUTPUT_DIR || `/tmp/tubeyou-ui-lint-${SESSION_ID}`;
const SCREENSHOT_DIR = path.join(OUTPUT_DIR, 'screenshots');
const RESULTS_PATH = path.join(OUTPUT_DIR, 'results.json');
function envFloat(name, defaultValue) {
    const raw = process.env[name];
    if (raw === undefined) return defaultValue;
    const value = Number(raw);
    if (!Number.isFinite(value)) {
        throw new Error(`Environment variable ${name} must be a finite number, got ${raw}`);
    }
    return value;
}

function envInt(name, defaultValue) {
    const raw = process.env[name];
    if (raw === undefined) return defaultValue;
    const value = Number(raw);
    if (!Number.isInteger(value)) {
        throw new Error(`Environment variable ${name} must be an integer, got ${raw}`);
    }
    return value;
}

const VISUAL_DRIFT_THRESHOLD = envFloat('UI_LINT_VISUAL_DRIFT_THRESHOLD', 0.008);
const MOBILE_TOUCH_TARGET_MIN = envInt('UI_LINT_TOUCH_TARGET_MIN', 40);
const DESKTOP_TOUCH_TARGET_MIN = envInt('UI_LINT_DESKTOP_TOUCH_TARGET_MIN', 32);
const SCREENSHOT_SETTLE_MS = envInt('UI_LINT_SCREENSHOT_SETTLE_MS', 500);
const HARDCODED_COLOR_FAIL_THRESHOLD = envInt('UI_LINT_HARDCODED_COLOR_FAIL_THRESHOLD', 40);
const TOKEN_VIOLATION_FAIL_THRESHOLD = envInt('UI_LINT_TOKEN_VIOLATION_FAIL_THRESHOLD', 0);
const LAYOUT_STABLE_EPSILON_PX = envInt('UI_LINT_LAYOUT_STABLE_EPSILON_PX', 1);
const MOBILE_LAYOUT_STABLE_FRAMES = envInt('UI_LINT_MOBILE_LAYOUT_STABLE_FRAMES', 4);
const DESKTOP_LAYOUT_STABLE_FRAMES = envInt('UI_LINT_DESKTOP_LAYOUT_STABLE_FRAMES', 2);
const MOBILE_LAYOUT_MAX_FRAMES = envInt('UI_LINT_MOBILE_LAYOUT_MAX_FRAMES', 36);
const DESKTOP_LAYOUT_MAX_FRAMES = envInt('UI_LINT_DESKTOP_LAYOUT_MAX_FRAMES', 16);
const MOTION_RESET_CSS = `
  *, *::before, *::after {
    animation: none !important;
    transition: none !important;
    scroll-behavior: auto !important;
  }
`;
const VISUAL_STABILITY_CSS = `
    /* Preserve actual fonts for accurate layout testing */
    img, video {
        visibility: hidden !important;
    }
`;
const MOBILE_VISUAL_STABILITY_CSS = `
    html {
        -webkit-text-size-adjust: 100% !important;
        text-size-adjust: 100% !important;
        scrollbar-gutter: stable !important;
    }

    body {
        overscroll-behavior: none !important;
    }
`;

if (!USERNAME || !PASSWORD) {
    console.error('Error: UI_LINT_USERNAME and UI_LINT_PASSWORD environment variables must be set');
    console.error('Example: export UI_LINT_USERNAME=admin && export UI_LINT_PASSWORD=your-password');
    process.exit(1);
}

const VIEW_DEFS = [
    {
        name: 'login',
        url: '/login',
        readySelector: '#login-form',
        auth: false,
        device: 'desktop',
        requiredSelectors: ['#username', '#password', '#submit-btn'],
    },
    {
        name: 'dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'desktop',
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsTable', '#wsIndicator'],
    },
    {
        name: 'settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'desktop',
        requiredSelectors: ['#settingsForm', '#settingsSaveBtn', '#lalalAuthBtn'],
    },
    {
        name: 'mobile-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'mobile',
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsTable'],
    },
    {
        name: 'mobile-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'mobile',
        requiredSelectors: ['#settingsForm', '#settingsSaveBtn'],
    },
];

const LOGIN_VIEW = VIEW_DEFS.find((view) => view.name === 'login');
if (!LOGIN_VIEW) {
    throw new Error("VIEW_DEFS must contain a view named 'login'");
}

/**
 * Returns true when urlStr belongs to the same origin as BASE_URL.
 * Swallows malformed URLs and returns false.
 * @param {string} urlStr
 * @returns {boolean}
 */
const isSameOrigin = (urlStr) => {
    try {
        return new URL(urlStr).origin === BASE_ORIGIN;
    } catch {
        return false;
    }
};

function createContextOptions(device) {
    if (device === 'mobile') {
        const iphone13 = devices['iPhone 13'];
        return {
            ...iphone13,
            viewport: { ...iphone13.viewport },
            screen: { ...iphone13.screen },
            colorScheme: 'dark',
            locale: 'en-US',
        };
    }

    return {
        viewport: { width: 1440, height: 1200 },
        screen: { width: 1440, height: 1200 },
        deviceScaleFactor: 1,
        hasTouch: false,
        colorScheme: 'dark',
    };
}

/**
 * Formats a result object into a compact single-line summary string for console output.
 * @param {object} result
 * @returns {string}
 */
function formatResultSummary(result) {
    const parts = [];
    if (result.failures.length) parts.push(`failures=${result.failures.length}`);
    if (result.warnings.length) parts.push(`warnings=${result.warnings.length}`);
    const metrics = result.metrics || {};
    if (metrics.duplicateIds) parts.push(`duplicateIds=${metrics.duplicateIds}`);
    if (metrics.unlabeledControls) parts.push(`unlabeledControls=${metrics.unlabeledControls}`);
    if (metrics.contrastIssues) parts.push(`contrastIssues=${metrics.contrastIssues}`);
    if (metrics.layoutDriftIssues) parts.push(`layoutDriftIssues=${metrics.layoutDriftIssues}`);
    if (metrics.unstyledDisabledControls) parts.push(`unstyledDisabled=${metrics.unstyledDisabledControls}`);
    if (metrics.disabledWithoutStyle) parts.push(`disabledWithoutStyle=${metrics.disabledWithoutStyle}`);
    if (metrics.badInputs) parts.push(`badInputs=${metrics.badInputs}`);
    if (metrics.inconsistentButtons) parts.push('inconsistentButtons=true');
    if (metrics.tinyText) parts.push(`tinyText=${metrics.tinyText}`);
    if (metrics.weakText) parts.push(`weakText=${metrics.weakText}`);
    if (metrics.spacingIssues) parts.push(`spacingIssues=${metrics.spacingIssues}`);
    if (metrics.alignmentIssues) parts.push(`alignmentIssues=${metrics.alignmentIssues}`);
    if (metrics.hardcodedColors) parts.push(`hardcodedColors=${metrics.hardcodedColors}`);
    if (metrics.badIconButtons) parts.push(`badIconButtons=${metrics.badIconButtons}`);
    if (metrics.tokenViolations) parts.push(`tokenViolations=${metrics.tokenViolations}`);
    if (metrics.iconButtonsWithoutAria) parts.push(`iconButtonsNoAria=${metrics.iconButtonsWithoutAria}`);
    if (metrics.unlabeledInputsStrict) parts.push(`unlabeledInputs=${metrics.unlabeledInputsStrict}`);
    if (metrics.legacyClassViolations?.length) parts.push(`legacyClasses=${metrics.legacyClassViolations.join('|')}`);
    if (metrics.insufficientFixedTopOffset) parts.push('navOffset=bad');
    if (metrics.footerNotFlex) parts.push('footerFlex=bad');
    if (metrics.mutationObservers) parts.push(`mutationObservers=${metrics.mutationObservers}`);
    if (metrics.duplicateEventHandlers) parts.push(`duplicateGlobalClicks=${metrics.duplicateEventHandlers}`);
    if (metrics.visualDriftRatio > 0) parts.push(`visualDrift=${metrics.visualDriftRatio.toFixed(5)}`);
    // New checks from CSS review
    if (metrics.undefinedCustomProperties) parts.push(`undefinedVars=${metrics.undefinedCustomProperties}`);
    if (metrics.backdropFilterCount > 4) parts.push(`backdropFilters=${metrics.backdropFilterCount}`);
    if (metrics.stickyWithoutBackground) parts.push(`stickyNoBackground=${metrics.stickyWithoutBackground}`);
    if (!metrics.hasFocusVisibleRules) parts.push('noFocusVisible=true');
    if (metrics.noVisibleFocusIndicators) parts.push(`noFocusRing=${metrics.noVisibleFocusIndicators}`);
    if (metrics.unguardedAnimations) parts.push(`unguardedAnimations=${metrics.unguardedAnimations}`);
    if (metrics.clippedDropdowns) parts.push(`clippedDropdowns=${metrics.clippedDropdowns}`);
    if (metrics.importantAbuse) parts.push(`importantAbuse=${metrics.importantAbuse}`);
    if (metrics.tightlyPackedTargets) parts.push(`tightTargets=${metrics.tightlyPackedTargets}`);
    return parts.join(' ');
}

/**
 * Zero-value metrics skeleton shared by skipped and runner-error results.
 * Always spread before adding result-specific flags.
 */
const BASE_METRICS = Object.freeze({
    duplicateIds: 0,
    unlabeledControls: 0,
    missingSelectors: 0,
    missingSelectorList: [],
    horizontalOverflow: false,
    overflowAmount: 0,
    smallTouchTargets: 0,
    tightlyPackedTargets: 0,
    contrastIssues: 0,
    layoutDriftIssues: 0,
    unstyledDisabledControls: 0,
    disabledWithoutStyle: 0,
    badInputs: 0,
    inconsistentButtons: false,
    tinyText: 0,
    weakText: 0,
    spacingIssues: 0,
    alignmentIssues: 0,
    hardcodedColors: 0,
    badIconButtons: 0,
    tokenViolations: 0,
    iconButtonsWithoutAria: 0,
    unlabeledInputsStrict: 0,
    legacyClassViolations: [],
    hasMainLandmark: true,
    hasAppShell: true,
    insufficientFixedTopOffset: false,
    footerNotFlex: false,
    mutationObservers: 0,
    duplicateEventHandlers: 0,
    visualDriftRatio: 0,
    // New checks from CSS review
    undefinedCustomProperties: 0,
    backdropFilterCount: 0,
    stickyWithoutBackground: 0,
    hasFocusVisibleRules: true,
    noVisibleFocusIndicators: 0,
    unguardedAnimations: 0,
    clippedDropdowns: 0,
    importantAbuse: 0,
    fontLoadingStatus: 'unknown',
});

/**
 * Build a placeholder result for a view that was intentionally skipped.
 * @param {string} name
 * @param {string} url
 * @param {string} warning  Human-readable reason for skipping.
 * @returns {object}
 */
function buildSkippedResult(name, url, warning) {
    return {
        name,
        url,
        failures: [],
        warnings: [warning],
        metrics: { ...BASE_METRICS, skipped: true },
    };
}

/**
 * Checks whether the app requires authentication by probing the /login route.
 * @returns {Promise<boolean>}
 */
async function detectLoginRequired() {
    try {
        const response = await fetch(`${BASE_URL}/login`, { redirect: 'manual' });
        return response.status === 200;
    } catch {
        return true;
    }
}

/**
 * Partitions network traffic into same-origin failures (hard) and external
 * warnings (soft). The optional ignoreBadResponse predicate can suppress
 * known-benign status codes such as 401 on the login endpoint.
 * @param {object} traffic
 * @param {{ignoreBadResponse?: (entry: object) => boolean}} [options]
 * @returns {{sameOriginFailures: string[], externalWarnings: string[]}}
 */
function splitNetworkFindings(traffic, options = {}) {
    const sameOriginFailures = [];
    const externalWarnings = [];
    const ignoreBadResponse = options.ignoreBadResponse || (() => false);

    for (const entry of traffic.requestFailures) {
        const bucket = isSameOrigin(entry.url) ? sameOriginFailures : externalWarnings;
        bucket.push(`requestfailed ${entry.url} (${entry.error})`);
    }

    for (const entry of traffic.badResponses) {
        if (ignoreBadResponse(entry)) {
            continue;
        }
        const bucket = isSameOrigin(entry.url) ? sameOriginFailures : externalWarnings;
        bucket.push(`response ${entry.status} ${entry.url}`);
    }

    return { sameOriginFailures, externalWarnings };
}

/**
 * Captures two full-page screenshots after the layout has stabilized,
 * separated by a configurable settle delay.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<{shotA: string, shotB: string}>}
 */
async function captureStablePair(page, view) {
    const safeName = sanitize(view.name);
    const shotA = path.join(SCREENSHOT_DIR, `${safeName}-a.png`);
    const shotB = path.join(SCREENSHOT_DIR, `${safeName}-b.png`);

    await waitForLayoutStability(page, view);
    await page.waitForTimeout(SCREENSHOT_SETTLE_MS);
    await page.screenshot({ path: shotA, fullPage: true, animations: 'disabled' });
    await waitForLayoutStability(page, view);
    await page.waitForTimeout(SCREENSHOT_SETTLE_MS);
    await page.screenshot({ path: shotB, fullPage: true, animations: 'disabled' });

    return { shotA, shotB };
}

/**
 * Polls successive animation frames until viewport geometry has been stable
 * for the required number of consecutive frames or the frame budget runs out.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<void>}
 */
async function waitForLayoutStability(page, view) {
    await page.evaluate(async ({ stableFrames, maxFrames, epsilon }) => {
        const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));
        const root = document.documentElement;
        const read = () => {
            const body = document.body;
            const viewport = window.visualViewport;
            return {
                innerWidth: Math.round(window.innerWidth),
                innerHeight: Math.round(window.innerHeight),
                scrollX: Math.round(window.scrollX),
                scrollY: Math.round(window.scrollY),
                rootClientWidth: Math.round(root?.clientWidth || 0),
                rootClientHeight: Math.round(root?.clientHeight || 0),
                rootScrollWidth: Math.round(root?.scrollWidth || 0),
                rootScrollHeight: Math.round(root?.scrollHeight || 0),
                bodyScrollWidth: Math.round(body?.scrollWidth || 0),
                bodyScrollHeight: Math.round(body?.scrollHeight || 0),
                viewportWidth: Math.round(viewport?.width || 0),
                viewportHeight: Math.round(viewport?.height || 0),
                viewportOffsetTop: Math.round(viewport?.offsetTop || 0),
                viewportOffsetLeft: Math.round(viewport?.offsetLeft || 0),
                viewportScale: Number((viewport?.scale || 1).toFixed(3)),
            };
        };

        const isStable = (previous, current) => Object.keys(previous).every((key) => {
            const prevValue = previous[key];
            const currentValue = current[key];
            if (typeof prevValue === 'number' && typeof currentValue === 'number') {
                return Math.abs(prevValue - currentValue) <= epsilon;
            }
            return prevValue === currentValue;
        });

        if (document.fonts?.ready) {
            await document.fonts.ready.catch(() => {});
        }

        if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }

        window.scrollTo(0, 0);

        let previous = read();
        let stableCount = 0;

        for (let index = 0; index < maxFrames; index += 1) {
            await nextFrame();
            await nextFrame();
            window.scrollTo(0, 0);

            if (document.activeElement instanceof HTMLElement) {
                document.activeElement.blur();
            }

            const current = read();
            stableCount = isStable(previous, current) ? stableCount + 1 : 0;
            previous = current;

            if (stableCount >= stableFrames) {
                return current;
            }
        }

        return previous;
    }, {
        stableFrames: view.device === 'mobile' ? MOBILE_LAYOUT_STABLE_FRAMES : DESKTOP_LAYOUT_STABLE_FRAMES,
        maxFrames: view.device === 'mobile' ? MOBILE_LAYOUT_MAX_FRAMES : DESKTOP_LAYOUT_MAX_FRAMES,
        epsilon: LAYOUT_STABLE_EPSILON_PX,
    });
}

/**
 * Runs all accessibility, design-system, and performance metric checks
 * against the current page state inside the browser context.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<object>} Raw metric data (arrays, not counts).
 */
async function collectMetrics(page, view) {
    let timeoutId;
    const evaluatePromise = page.evaluate(async ({ requiredSelectors, mobileTouchTargetMin, desktopTouchTargetMin, isMobile }) => {
        const interactiveSelector = [
            'a[href]',
            'button',
            'input:not([type="hidden"])',
            'select',
            'textarea',
            '[role="button"]',
        ].join(', ');

        const isVisible = (el) => {
            if (!el || !el.isConnected) return false;
            if (el.closest('[hidden], .d-none, [aria-hidden="true"]')) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        const accessibleName = (el) => {
            const ariaLabel = el.getAttribute('aria-label');
            if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                const text = labelledBy
                    .split(/\s+/)
                    .map((id) => document.getElementById(id)?.textContent?.trim() || '')
                    .join(' ')
                    .trim();
                if (text) return text;
            }

            if (el.id) {
                const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (label?.textContent?.trim()) return label.textContent.trim();
            }

            if (el.textContent?.trim()) return el.textContent.trim();
            if (el.getAttribute('title')?.trim()) return el.getAttribute('title').trim();
            if (el.getAttribute('placeholder')?.trim()) return el.getAttribute('placeholder').trim();
            return '';
        };

        const duplicateIds = [];
        const seenIds = new Set();
        for (const el of document.querySelectorAll('[id]')) {
            if (seenIds.has(el.id)) duplicateIds.push(el.id);
            seenIds.add(el.id);
        }

        const unlabeledControls = Array.from(document.querySelectorAll(interactiveSelector))
            .filter((el) => isVisible(el) && !el.disabled)
            .filter((el) => !accessibleName(el))
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const missingSelectors = requiredSelectors.filter((selector) => !document.querySelector(selector));
        const overflowAmount = Math.max(0, document.documentElement.scrollWidth - window.innerWidth);
        const horizontalOverflow = overflowAmount > 1;

        const touchTargetMin = isMobile ? mobileTouchTargetMin : desktopTouchTargetMin;
        const smallTouchTargets = Array.from(document.querySelectorAll(interactiveSelector))
            .filter((el) => isVisible(el) && !el.disabled)
            .map((el) => {
                const rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                };
            })
            .filter((entry) => entry.width < touchTargetMin || entry.height < touchTargetMin);

        const parseColor = (value) => {
            if (!value || value === 'transparent') return null;
            const match = value.match(/rgba?\(([^)]+)\)/i);
            if (!match) return null;
            const parts = match[1].split(',').map((p) => p.trim());
            const r = Number(parts[0]);
            const g = Number(parts[1]);
            const b = Number(parts[2]);
            const a = parts[3] === undefined ? 1 : Number(parts[3]);
            if ([r, g, b, a].some((n) => Number.isNaN(n))) return null;
            return { r, g, b, a };
        };

        const relativeLuminance = ({ r, g, b }) => {
            const srgb = [r, g, b].map((v) => {
                const c = v / 255;
                return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * srgb[0] + 0.7152 * srgb[1] + 0.0722 * srgb[2];
        };

        const contrastRatio = (fg, bg) => {
            const l1 = relativeLuminance(fg);
            const l2 = relativeLuminance(bg);
            const lighter = Math.max(l1, l2);
            const darker = Math.min(l1, l2);
            return (lighter + 0.05) / (darker + 0.05);
        };

        // Alpha compositing for semi-transparent backgrounds (Porter-Duff)
        const compositeOver = (fg, bg) => {
            const a = fg.a + bg.a * (1 - fg.a);
            if (a === 0) return { r: 0, g: 0, b: 0, a: 0 };
            return {
                r: Math.round((fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / a),
                g: Math.round((fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / a),
                b: Math.round((fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / a),
                a,
            };
        };

        // Get effective background color with alpha compositing along parent chain
        const getEffectiveBackground = (el) => {
            const chain = [];
            let current = el;
            while (current && current !== document.documentElement) {
                const bg = parseColor(window.getComputedStyle(current).backgroundColor);
                if (bg && bg.a > 0) chain.push(bg);
                current = current.parentElement;
            }
            // Dark theme base color fallback
            let composite = { r: 15, g: 23, b: 42, a: 1 };
            for (let i = chain.length - 1; i >= 0; i--) {
                composite = compositeOver(chain[i], composite);
            }
            return composite;
        };

        const contrastIssues = [];
        const contrastCandidates = document.querySelectorAll('p, span, a, button, label, td, th, li');
        for (const el of contrastCandidates) {
            if (!isVisible(el)) continue;

            const text = el.textContent?.trim();
            if (!text || text.length < 2) continue;

            const style = window.getComputedStyle(el);
            const fg = parseColor(style.color);
            if (!fg) continue;

            // Use alpha-composited effective background
            const bg = getEffectiveBackground(el);
            if (!bg || bg.a === 0) continue;

            if (contrastRatio(fg, bg) < 4.5) {
                contrastIssues.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    ratio: contrastRatio(fg, bg).toFixed(2),
                });
            }
        }

        const layoutDriftIssues = [];
        const candidates = document.querySelectorAll('nav, header, [class*="nav"]');
        const fixedTops = Array.from(candidates).filter((el) => {
            if (!isVisible(el)) return false;
            const style = window.getComputedStyle(el);
            if (style.position !== 'fixed' && style.position !== 'sticky') return false;
            const rect = el.getBoundingClientRect();
            return rect.top <= 0 && rect.height > 0;
        });
        const topBarBottom = fixedTops.reduce((maxBottom, el) => {
            const rect = el.getBoundingClientRect();
            return Math.max(maxBottom, rect.bottom);
        }, 0);

        const firstContent =
            document.querySelector('main') ||
            document.querySelector('[role="main"]') ||
            document.querySelector('.container');
        if (firstContent && topBarBottom > 0) {
            const contentTop = firstContent.getBoundingClientRect().top;
            if (contentTop < topBarBottom + 8) {
                layoutDriftIssues.push({
                    type: 'fixed-header-overlap',
                    overlapPx: Math.round(topBarBottom + 8 - contentTop),
                });
            }
        }

        const disabledControls = Array.from(document.querySelectorAll('input:disabled, select:disabled, textarea:disabled, button:disabled'));
        const unstyledDisabledControls = disabledControls
            .filter((el) => isVisible(el))
            .filter((el) => {
                const style = window.getComputedStyle(el);
                const bg = parseColor(style.backgroundColor);
                if (!bg || bg.a === 0) {
                    return false;
                }

                const luma = (0.2126 * bg.r + 0.7152 * bg.g + 0.0722 * bg.b) / 255;
                return luma >= 0.8;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const disabledWithoutStyle = disabledControls
            .filter((el) => isVisible(el))
            .filter((el) => {
                const style = window.getComputedStyle(el);
                const opacity = Number.parseFloat(style.opacity || '1');
                const bgRaw = style.backgroundColor || '';
                const bgParsed = parseColor(bgRaw);
                const isTransparent = bgRaw === 'transparent' || (bgParsed && bgParsed.a === 0);
                const isBright = bgRaw.includes('255, 255, 255') || bgRaw.includes('rgb(255');

                if (opacity < 0.999) return false;
                return Boolean(isTransparent || isBright);
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        // Script-level static checks for global behavior and potential perf hotspots.
        const scriptChunks = [];
        for (const script of Array.from(document.scripts)) {
            const inline = script.textContent?.trim() || '';
            if (inline) {
                scriptChunks.push(inline);
                continue;
            }

            const src = script.getAttribute('src') || '';
            if (!src) continue;

            try {
                const absolute = new URL(src, window.location.href);
                if (absolute.origin !== window.location.origin) continue;
                const response = await fetch(absolute.href, { credentials: 'same-origin' });
                if (!response.ok) continue;
                scriptChunks.push(await response.text());
            } catch {
                // Ignore script fetch issues and continue with available sources.
            }
        }

        const joinedScripts = scriptChunks.join('\n');
        const mutationObservers = (joinedScripts.match(/\bMutationObserver\b/g) || []).length;
        const duplicateEventHandlers = (joinedScripts.match(/(?:document|window)\.addEventListener\(\s*['"]click['"]/g) || []).length;

        // Design-system checks
        const bodyBg = parseColor(window.getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
        const bodyLuma = (0.2126 * bodyBg.r + 0.7152 * bodyBg.g + 0.0722 * bodyBg.b) / 255;
        const isDarkUi = bodyLuma < 0.45;

        const badInputs = Array.from(document.querySelectorAll('input, select, textarea'))
            .filter((el) => isVisible(el))
            .filter((el) => {
                if (!isDarkUi) return false;
                const bg = window.getComputedStyle(el).backgroundColor || '';
                return bg.includes('255, 255, 255') || bg.includes('rgb(255');
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const buttonRects = Array.from(document.querySelectorAll('.btn, button'))
            .filter((el) => isVisible(el))
            .map((el) => el.getBoundingClientRect());

        let inconsistentButtons = false;
        if (buttonRects.length > 2) {
            const heights = buttonRects.map((rect) => Math.round(rect.height));
            const uniqueHeights = new Set(heights);
            inconsistentButtons = uniqueHeights.size > 2;
        }

        // Broader selector for tiny text detection
        const tinyTextSelector = [
            'p', 'span', 'label', 'small', 'a', 'button',
            'td', 'th', 'li', 'dt', 'dd',
            '[class*="pill"]', '[class*="badge"]', '[class*="title"]',
            '[class*="label"]', '[class*="hint"]',
        ].join(', ');

        const tinyText = Array.from(document.querySelectorAll(tinyTextSelector))
            .filter((el) => isVisible(el))
            .filter((el) => {
                const size = parseFloat(window.getComputedStyle(el).fontSize);
                return Number.isFinite(size) && size < 12;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className.split(' ')[0] : '',
            }));

        // Luminance-based weak text detection (contrast ratio between 2.0 and 4.5)
        const weakText = Array.from(
            document.querySelectorAll('p, span, label, small, .text-muted, [class*="muted"]')
        )
            .filter((el) => isVisible(el))
            .filter((el) => {
                const fg = parseColor(window.getComputedStyle(el).color);
                if (!fg) return false;
                const bg = getEffectiveBackground(el);
                if (!bg || bg.a === 0) return false;
                const ratio = contrastRatio(fg, bg);
                // Weak = between barely readable and AA threshold
                return ratio >= 2.0 && ratio < 4.5;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                ratio: contrastRatio(
                    parseColor(window.getComputedStyle(el).color),
                    getEffectiveBackground(el)
                ).toFixed(2),
            }));

        const spacingIssues = [];
        const blocks = Array.from(document.querySelectorAll('.ui-card, .btn, input, .form-control'))
            .filter((el) => isVisible(el))
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                if (Math.abs(ar.top - br.top) > 2) return ar.top - br.top;
                return ar.left - br.left;
            });
        for (let i = 0; i < blocks.length - 1; i += 1) {
            const aRect = blocks[i].getBoundingClientRect();
            const bRect = blocks[i + 1].getBoundingClientRect();
            const overlapX = Math.max(0, Math.min(aRect.right, bRect.right) - Math.max(aRect.left, bRect.left));
            const minWidth = Math.max(1, Math.min(aRect.width, bRect.width));
            const horizontalMatch = overlapX / minWidth > 0.4;
            const gap = bRect.top - aRect.bottom;

            if (horizontalMatch && gap > -1 && gap < 2) {
                spacingIssues.push({
                    firstTag: blocks[i].tagName.toLowerCase(),
                    secondTag: blocks[i + 1].tagName.toLowerCase(),
                });
            }
        }

        const alignmentIssues = [];
        const leftEdges = Array.from(document.querySelectorAll('.ui-card, .form-control'))
            .filter((el) => isVisible(el))
            .map((el) => Math.round(el.getBoundingClientRect().left));

        if (leftEdges.length > 3) {
            const sorted = [...leftEdges].sort((a, b) => a - b);
            const median = sorted[Math.floor(sorted.length / 2)];
            const outliers = leftEdges.filter((left) => Math.abs(left - median) > 8);
            if (outliers.length / leftEdges.length > 0.35) {
                alignmentIssues.push({ outlierRatio: outliers.length / leftEdges.length });
            }
        }

        const hardcodedColors = [];
        const hardcodedColorPattern = /(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)|\b(?:white|black)\b)/i;
        for (const el of document.querySelectorAll('[style], [bgcolor], [color], [fill], [stroke]')) {
            const styleAttr = el.getAttribute('style') || '';
            const bgAttr = el.getAttribute('bgcolor') || '';
            const colorAttr = el.getAttribute('color') || '';
            const fillAttr = el.getAttribute('fill') || '';
            const strokeAttr = el.getAttribute('stroke') || '';
            const combined = `${styleAttr} ${bgAttr} ${colorAttr} ${fillAttr} ${strokeAttr}`.trim();

            if (combined && hardcodedColorPattern.test(combined)) {
                hardcodedColors.push(el.tagName.toLowerCase());
            }
        }

        const getButtonLabelText = (buttonEl) => {
            const clone = buttonEl.cloneNode(true);
            clone.querySelectorAll('.material-symbols-outlined, .material-icons, [aria-hidden="true"]')
                .forEach((node) => node.remove());
            return (clone.textContent || '').trim();
        };

        const badIconButtons = Array.from(document.querySelectorAll('.btn'))
            .filter((el) => isVisible(el))
            .filter((el) => getButtonLabelText(el).length === 0)
            .filter((el) => {
                const rect = el.getBoundingClientRect();
                return rect.width < 32 || rect.height < 32;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const forbiddenColorRegex = /#([0-9a-f]{3,8})|rgba?\(|hsla?\(/i;
        const forbiddenSpacingRegex = /(margin|padding|gap|border-radius)\s*:\s*[-0-9.]+(px|rem|em|%)?/i;
        const tokenViolations = [];

        for (const el of document.querySelectorAll('[style], [bgcolor], [color], [fill], [stroke]')) {
            const styleAttr = el.getAttribute('style') || '';
            const presentational = [
                el.getAttribute('bgcolor') || '',
                el.getAttribute('color') || '',
                el.getAttribute('fill') || '',
                el.getAttribute('stroke') || '',
            ].join(' ');

            if (styleAttr) {
                if (forbiddenColorRegex.test(styleAttr) || forbiddenSpacingRegex.test(styleAttr)) {
                    tokenViolations.push({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                    });
                    continue;
                }
                if (styleAttr.includes(':') && !styleAttr.includes('var(')) {
                    tokenViolations.push({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                    });
                    continue;
                }
            }

            if (presentational && forbiddenColorRegex.test(presentational)) {
                tokenViolations.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                });
            }
        }

        const legacyClassSet = new Set();
        const forbiddenClasses = ['card', 'shadow-sm', 'settings-panel'];
        for (const el of document.querySelectorAll('*')) {
            for (const cls of forbiddenClasses) {
                if (el.classList.contains(cls)) {
                    legacyClassSet.add(cls);
                }
            }
        }
        const legacyClassViolations = [...legacyClassSet];

        const iconButtonsWithoutAria = Array.from(document.querySelectorAll('button'))
            .filter((btn) => isVisible(btn))
            .filter((btn) => getButtonLabelText(btn).length === 0)
            .filter((btn) => !btn.getAttribute('aria-label') && !btn.getAttribute('aria-labelledby'))
            .map((btn) => ({
                id: btn.id || null,
                className: typeof btn.className === 'string' ? btn.className : '',
            }));

        const unlabeledInputsStrict = Array.from(document.querySelectorAll('input, select, textarea'))
            .filter((el) => {
                if (!isVisible(el)) return false;
                if (el.tagName.toLowerCase() === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') {
                    return false;
                }
                return true;
            })
            .filter((el) => {
                const id = el.id;
                const ariaLabel = el.getAttribute('aria-label');
                const ariaLabelledby = el.getAttribute('aria-labelledby');
                const hasExplicitLabel = id ? Boolean(document.querySelector(`label[for="${CSS.escape(id)}"]`)) : false;
                return !hasExplicitLabel && !ariaLabel && !ariaLabelledby;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const hasMainLandmark = Boolean(document.querySelector('main'));
        const hasAppShell = Boolean(document.querySelector('.app-shell'));

        let insufficientFixedTopOffset = false;
        if (document.querySelector('.fixed-top')) {
            const bodyPaddingTop = parseInt(window.getComputedStyle(document.body).paddingTop || '0', 10);
            insufficientFixedTopOffset = Number.isFinite(bodyPaddingTop) && bodyPaddingTop < 40;
        }

        const footer = document.querySelector('.wb-footer-content');
        let footerNotFlex = false;
        if (footer) {
            const footerStyle = window.getComputedStyle(footer);
            footerNotFlex = footerStyle.display !== 'flex';
        }

        // ─── NEW CSS REVIEW CHECKS ────────────────────────────────────────────────

        // 1. Undefined CSS Custom Properties
        const undefinedCustomProperties = (() => {
            const defined = new Set();
            const used = new Map();
            const varPattern = /var\(\s*(--[\w-]+)/g;

            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        // Collect defined properties from :root
                        if (rule.selectorText === ':root' && rule.style) {
                            for (let i = 0; i < rule.style.length; i++) {
                                const p = rule.style[i];
                                if (p.startsWith('--')) defined.add(p);
                            }
                        }
                        // Collect all var() usages
                        if (!rule.style) continue;
                        const cssText = rule.style.cssText;
                        let match;
                        while ((match = varPattern.exec(cssText)) !== null) {
                            const prop = match[1];
                            if (!used.has(prop)) used.set(prop, []);
                            used.get(prop).push(rule.selectorText || '(unknown)');
                        }
                    }
                } catch { /* cross-origin sheet */ }
            }

            // Find used but undefined
            const undefined = [];
            for (const [prop, selectors] of used.entries()) {
                if (!defined.has(prop)) {
                    undefined.push({ property: prop, usedIn: selectors.slice(0, 3) });
                }
            }
            return undefined;
        })();

        // 2. Excessive backdrop-filter usage (performance)
        const backdropFilterCount = Array.from(document.querySelectorAll('*'))
            .filter(el => {
                if (!isVisible(el)) return false;
                const style = window.getComputedStyle(el);
                return style.backdropFilter && style.backdropFilter !== 'none';
            }).length;

        // 3. Sticky elements without solid background
        const stickyWithoutBackground = Array.from(
            document.querySelectorAll('th, [class*="header"], nav, thead')
        ).filter(el => {
            if (!isVisible(el)) return false;
            const style = window.getComputedStyle(el);
            if (style.position !== 'sticky' && style.position !== 'fixed') return false;
            const bg = parseColor(style.backgroundColor);
            return !bg || bg.a < 0.9;
        }).map(el => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            position: window.getComputedStyle(el).position,
        }));

        // 4. Focus-visible styles check
        const focusVisibleCheck = (() => {
            let hasFocusVisible = false;
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (rule.selectorText?.includes(':focus-visible')) {
                            hasFocusVisible = true;
                            break;
                        }
                    }
                } catch { /* cross-origin */ }
                if (hasFocusVisible) break;
            }

            const testElements = Array.from(
                document.querySelectorAll('a[href], button, input, select, textarea')
            ).filter(el => isVisible(el) && !el.disabled).slice(0, 10);

            const noVisibleFocus = [];
            for (const el of testElements) {
                el.focus();
                const style = window.getComputedStyle(el);
                const hasOutline = style.outlineStyle !== 'none' && style.outlineWidth !== '0px';
                const hasBoxShadow = style.boxShadow !== 'none';
                if (!hasOutline && !hasBoxShadow) {
                    noVisibleFocus.push({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                    });
                }
                el.blur();
            }

            return { hasFocusVisible, noVisibleFocus };
        })();

        // 5. Animations without prefers-reduced-motion guard
        const unguardedAnimations = (() => {
            const animatedProps = ['animation', 'animation-name', 'transition'];
            let hasReducedMotionMedia = false;
            const unguarded = [];

            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (rule instanceof CSSMediaRule) {
                            if (rule.conditionText?.includes('prefers-reduced-motion')) {
                                hasReducedMotionMedia = true;
                            }
                        }
                        if (!rule.style) continue;
                        for (const prop of animatedProps) {
                            const value = rule.style.getPropertyValue(prop);
                            if (value && value !== 'none' && !value.includes('0s')) {
                                const parent = rule.parentRule;
                                const isGuarded = parent instanceof CSSMediaRule
                                    && parent.conditionText?.includes('prefers-reduced-motion');
                                if (!isGuarded) {
                                    unguarded.push({
                                        selector: rule.selectorText || '(unknown)',
                                        property: prop,
                                    });
                                }
                            }
                        }
                    }
                } catch { /* cross-origin */ }
            }

            return { hasReducedMotionMedia, unguarded: unguarded.slice(0, 15) };
        })();

        // 6. Dropdowns inside overflow:hidden containers
        const clippedDropdowns = Array.from(
            document.querySelectorAll('.dropdown-menu, [role="menu"], [role="listbox"]')
        ).filter(el => {
            let parent = el.parentElement;
            while (parent && parent !== document.body) {
                const style = window.getComputedStyle(parent);
                const overflow = `${style.overflow} ${style.overflowX} ${style.overflowY}`;
                if (overflow.includes('hidden') || overflow.includes('clip')) {
                    return true;
                }
                parent = parent.parentElement;
            }
            return false;
        }).map(el => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
        }));

        // 7. !important abuse check
        const importantAbuse = (() => {
            let count = 0;
            const violations = [];
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (!rule.style) continue;
                        for (let i = 0; i < rule.style.length; i++) {
                            const prop = rule.style[i];
                            if (rule.style.getPropertyPriority(prop) === 'important') {
                                count++;
                                const sel = rule.selectorText || '';
                                // Only flag base selectors (no ID, no attribute, simple)
                                if (sel && !sel.includes('#') && !sel.includes('[')
                                    && (sel.split(/[\s>+~]/).length <= 2)) {
                                    violations.push({ selector: sel, property: prop });
                                }
                            }
                        }
                    }
                } catch { /* cross-origin */ }
            }
            return { count, violations: violations.slice(0, 20) };
        })();

        // 8. Tightly packed touch targets (WCAG 2.5.8)
        const tightlyPackedTargets = (() => {
            const targets = Array.from(document.querySelectorAll(interactiveSelector))
                .filter(el => isVisible(el) && !el.disabled)
                .map(el => ({ el, rect: el.getBoundingClientRect() }));

            const tight = [];
            for (let i = 0; i < targets.length; i++) {
                for (let j = i + 1; j < targets.length; j++) {
                    const a = targets[i].rect;
                    const b = targets[j].rect;

                    // Only check horizontally adjacent (same row ± 8px)
                    if (Math.abs(a.top - b.top) > 8) continue;

                    const gap = Math.max(0, b.left - a.right);
                    const bothSmall = (a.width < 44 || a.height < 44)
                        && (b.width < 44 || b.height < 44);

                    if (bothSmall && gap < 8) {
                        tight.push({
                            gap: Math.round(gap),
                            aTag: targets[i].el.tagName.toLowerCase(),
                            bTag: targets[j].el.tagName.toLowerCase(),
                        });
                    }
                }
            }
            return tight.slice(0, 10);
        })();

        // 9. Font loading status
        const fontLoadingStatus = (() => {
            const status = document.fonts.status;
            const allFonts = Array.from(document.fonts).map(f => ({
                family: f.family,
                status: f.status,
            }));
            const customFontLoaded = allFonts.some(
                f => (f.family.includes('Roboto') || f.family.includes('Inter'))
                    && f.status === 'loaded'
            );
            return { status, customFontLoaded, fontCount: allFonts.length };
        })();

        return {
            duplicateIds,
            unlabeledControls,
            missingSelectors,
            horizontalOverflow,
            overflowAmount,
            smallTouchTargets,
            contrastIssues,
            layoutDriftIssues,
            unstyledDisabledControls,
            disabledWithoutStyle,
            badInputs,
            inconsistentButtons,
            tinyText,
            weakText,
            spacingIssues,
            alignmentIssues,
            hardcodedColors,
            badIconButtons,
            tokenViolations,
            iconButtonsWithoutAria,
            unlabeledInputsStrict,
            legacyClassViolations,
            hasMainLandmark,
            hasAppShell,
            insufficientFixedTopOffset,
            footerNotFlex,
            mutationObservers,
            duplicateEventHandlers,
            // New CSS review checks
            undefinedCustomProperties,
            backdropFilterCount,
            stickyWithoutBackground,
            focusVisibleCheck,
            unguardedAnimations,
            clippedDropdowns,
            importantAbuse,
            tightlyPackedTargets,
            fontLoadingStatus,
        };
    }, {
        requiredSelectors: view.requiredSelectors,
        mobileTouchTargetMin: MOBILE_TOUCH_TARGET_MIN,
        desktopTouchTargetMin: DESKTOP_TOUCH_TARGET_MIN,
        isMobile: view.device === 'mobile',
    });

    // Playwright cannot cancel an in-flight page.evaluate; the timeout only bounds the caller.
    try {
        return await Promise.race([
            evaluatePromise,
            new Promise((_, reject) => {
                timeoutId = setTimeout(() => reject(new Error(`collectMetrics timed out for ${view.name}`)), 30000);
            }),
        ]);
    } finally {
        if (timeoutId) {
            clearTimeout(timeoutId);
        }
    }
}

/**
 * Navigates to a view URL, waits for the ready selector, injects stability
 * CSS, and waits for layout to settle before returning.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @param {Record<string, string>} [replacements]  URL path parameter substitutions.
 * @returns {Promise<void>}
 */
async function openView(page, view, replacements = {}) {
    let url = view.url;
    for (const [key, value] of Object.entries(replacements)) {
        url = url.replaceAll(`:${key}`, String(value));
    }

    await page.goto(new URL(url, BASE_URL).href, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForSelector(view.readySelector, { timeout: 10000 });
    await disableMotion(page, MOTION_RESET_CSS, view.name);
    await page.addStyleTag({ content: VISUAL_STABILITY_CSS });
    if (view.device === 'mobile') {
        await page.addStyleTag({ content: MOBILE_VISUAL_STABILITY_CSS });
    }
    await waitForLayoutStability(page, view);
    await page.waitForTimeout(SCREENSHOT_SETTLE_MS);
}

/**
 * Authenticates once and returns the serialized storage state (cookies +
 * localStorage) that can be reused across view contexts.
 * @param {import('playwright').Browser} browser
 * @returns {Promise<object>} Playwright storage state.
 */
async function createAuthState(browser) {
    const context = await browser.newContext(createContextOptions('desktop'));
    const page = await context.newPage();

    await login(page, {
        baseUrl: BASE_URL,
        username: USERNAME,
        password: PASSWORD,
        motionResetCss: MOTION_RESET_CSS,
    });

    const state = await context.storageState();
    await context.close();
    return state;
}

/**
 * Build a result object for a view that failed with an unexpected runner exception.
 * @param {string} name
 * @param {string} url
 * @param {unknown} error
 * @returns {object}
 */
function buildRunnerErrorResult(name, url, error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
        name,
        url,
        failures: [`runner exception: ${message}`],
        warnings: [],
        metrics: { ...BASE_METRICS, hasMainLandmark: false, hasAppShell: false, runnerError: true },
        screenshots: {},
    };
}

/**
 * Wraps runView and catches any unhandled exception, returning a runner-error
 * result instead of propagating the rejection.
 * @param {import('playwright').Browser} browser
 * @param {object} storageState
 * @param {object} view
 * @param {Record<string, string>} [replacements]
 * @returns {Promise<object>}
 */
async function runViewSafely(browser, storageState, view, replacements = {}) {
    try {
        return await runView(browser, storageState, view, replacements);
    } catch (error) {
        return buildRunnerErrorResult(view.name, `${BASE_URL}${view.url}`, error);
    }
}

/**
 * Fetches the first available job ID via the API so the job-detail view can
 * be linted with real data. Returns null if none exist or the request fails.
 * @param {import('playwright').Browser} browser
 * @param {object} storageState
 * @returns {Promise<string|null>}
 */
async function discoverJobId(browser, storageState) {
    const context = await browser.newContext({ storageState });

    try {
        const response = await context.request.get(`${BASE_URL}/api/jobs`);
        if (!response.ok()) {
            return null;
        }
        const data = await response.json();

        // Handle both array response and nested { jobs: [...] } structure
        const jobsArray = Array.isArray(data)
            ? data
            : Array.isArray(data?.jobs) ? data.jobs : [];
        const firstJob = jobsArray[0];
        return firstJob?.id ?? firstJob?.job_id ?? null;
    } finally {
        await context.close();
    }
}

/**
 * Verifies that an invalid login attempt stays on /login and shows an error
 * message. Skipped automatically when authentication is disabled.
 * @param {import('playwright').Browser} browser
 * @param {boolean} loginRequired
 * @returns {Promise<object>}
 */
async function runInvalidLoginCheck(browser, loginRequired) {
    if (!loginRequired) {
        return buildSkippedResult(
            'login-error',
            `${BASE_URL}/login`,
            'login is disabled; skipped invalid-login check',
        );
    }

    const context = await browser.newContext(createContextOptions('desktop'));
    const page = await context.newPage();
    const stopCollecting = collectConsoleAndNetwork(page);

    try {
        await openView(page, LOGIN_VIEW);
        await page.fill('#username', USERNAME);
        await page.fill('#password', `${PASSWORD}__ui_lint_invalid`);
        await page.click('#submit-btn');
        await page.locator('#login-message.error').waitFor({ state: 'visible', timeout: 10000 });

        const metrics = await page.evaluate(() => ({
            errorVisible: document.querySelector('#login-message')?.classList.contains('error') ?? false,
            errorText: document.querySelector('#message-text')?.textContent?.trim() || '',
            stayedOnLogin: window.location.pathname === '/login',
        }));

        const traffic = stopCollecting();
        const failures = [];
        const warnings = [];
        const { sameOriginFailures, externalWarnings } = splitNetworkFindings(traffic, {
            ignoreBadResponse: (entry) => {
                try {
                    const url = new URL(entry.url, BASE_URL);
                    return url.pathname === '/login' && entry.status === 401;
                } catch {
                    return false;
                }
            },
        });

        if (!metrics.errorVisible) failures.push('invalid login did not show an error state');
        if (!metrics.errorText || !/error|invalid|failed/i.test(metrics.errorText)) {
            failures.push('invalid login did not show an expected error message');
        }
        if (!metrics.stayedOnLogin) failures.push('invalid login navigated away from /login');
        failures.push(...sameOriginFailures);
        if (traffic.pageErrors.length) {
            failures.push(...traffic.pageErrors.map((entry) => `pageerror ${entry}`));
        }
        if (traffic.consoleEntries.length) {
            warnings.push(...traffic.consoleEntries.map((entry) => `console ${entry.type}: ${entry.text}`));
        }
        warnings.push(...externalWarnings);

        return {
            name: 'login-error',
            url: `${BASE_URL}/login`,
            failures,
            warnings,
            metrics: {
                loginError: metrics,
            },
        };
    } finally {
        await context.close();
    }
}

/**
 * Runs the full lint suite for a single view: navigation, metric collection,
 * screenshot diffing, and network analysis.
 * @param {import('playwright').Browser} browser
 * @param {object} storageState
 * @param {object} view
 * @param {Record<string, string>} [replacements]  URL path parameter substitutions.
 * @returns {Promise<object>}
 */
async function runView(browser, storageState, view, replacements = {}) {
    const context = await browser.newContext({
        ...createContextOptions(view.device),
        ...(view.auth ? { storageState } : {}),
    });
    const page = await context.newPage();
    const stopCollecting = collectConsoleAndNetwork(page);

    try {
        await openView(page, view, replacements);

        const metrics = await collectMetrics(page, view);
        const shots = await captureStablePair(page, view);
        const visual = diffScreenshots({
            name: view.name,
            shotA: shots.shotA,
            shotB: shots.shotB,
            screenshotDir: SCREENSHOT_DIR,
        });
        const traffic = stopCollecting();

        const failures = [];
        const warnings = [];
        const { sameOriginFailures, externalWarnings } = splitNetworkFindings(traffic);

        if (metrics.duplicateIds.length) failures.push(`duplicate ids: ${metrics.duplicateIds.join(', ')}`);
        if (metrics.unlabeledControls.length) failures.push(`unlabeled controls: ${metrics.unlabeledControls.length}`);
        if (metrics.missingSelectors.length) failures.push(`missing selectors: ${metrics.missingSelectors.join(', ')}`);
        if (metrics.horizontalOverflow) failures.push(`horizontal overflow: ${metrics.overflowAmount}px`);
        if (metrics.smallTouchTargets.length) warnings.push(`undersized ${view.device} targets: ${metrics.smallTouchTargets.length}`);
        if (metrics.contrastIssues.length) warnings.push(`contrast issues: ${metrics.contrastIssues.length}`);
        if (metrics.layoutDriftIssues.length) warnings.push(`layout drift issues: ${metrics.layoutDriftIssues.length}`);
        if (metrics.unstyledDisabledControls?.length) warnings.push(`unstyled disabled controls: ${metrics.unstyledDisabledControls.length}`);
        if (metrics.badInputs.length) failures.push(`white inputs detected: ${metrics.badInputs.length}`);
        if (metrics.disabledWithoutStyle.length) failures.push(`disabled inputs without style: ${metrics.disabledWithoutStyle.length}`);
        if (metrics.inconsistentButtons) warnings.push('inconsistent button sizes');
        if (metrics.tinyText.length) warnings.push(`tiny unreadable text: ${metrics.tinyText.length}`);
        if (metrics.weakText.length > 5) warnings.push('too much weak contrast text');
        if (metrics.spacingIssues.length > 5) warnings.push('layout spacing collapsed');
        if (metrics.alignmentIssues.length) warnings.push('grid alignment inconsistent');
        if (metrics.hardcodedColors.length > HARDCODED_COLOR_FAIL_THRESHOLD) {
            failures.push('too many hardcoded colors (missing design tokens)');
        }
        if (metrics.badIconButtons.length) warnings.push(`invalid icon buttons: ${metrics.badIconButtons.length}`);
        if (metrics.tokenViolations.length > TOKEN_VIOLATION_FAIL_THRESHOLD) {
            failures.push(`design token violations: ${metrics.tokenViolations.length}`);
        }
        if (metrics.iconButtonsWithoutAria.length) {
            failures.push(`icon buttons without aria-label: ${metrics.iconButtonsWithoutAria.length}`);
        }
        if (metrics.unlabeledInputsStrict.length) {
            failures.push(`inputs without label: ${metrics.unlabeledInputsStrict.length}`);
        }
        if (metrics.legacyClassViolations.length) {
            warnings.push(`legacy classes used: ${metrics.legacyClassViolations.join(', ')}`);
        }
        if (!metrics.hasMainLandmark) failures.push('missing main landmark');
        if (!metrics.hasAppShell) failures.push('missing app-shell layout container');
        if (metrics.insufficientFixedTopOffset) failures.push('fixed navbar without sufficient body offset');
        if (metrics.footerNotFlex) warnings.push('footer not using flex layout');
        if (metrics.mutationObservers > 0) warnings.push('mutation observer usage detected');
        if (metrics.duplicateEventHandlers > 2) warnings.push('multiple global click handlers');
        if (visual.ratio > VISUAL_DRIFT_THRESHOLD) failures.push(`visual drift ratio ${visual.ratio.toFixed(5)} > ${VISUAL_DRIFT_THRESHOLD}`);

        // New CSS review checks
        if (metrics.undefinedCustomProperties?.length) {
            failures.push(`undefined CSS custom properties: ${metrics.undefinedCustomProperties.map(p => p.property).join(', ')}`);
        }
        if (metrics.backdropFilterCount > 4) {
            warnings.push(`excessive backdrop-filter usage: ${metrics.backdropFilterCount} simultaneous layers`);
        }
        if (metrics.stickyWithoutBackground?.length) {
            warnings.push(`sticky elements without solid background: ${metrics.stickyWithoutBackground.length}`);
        }
        if (!metrics.focusVisibleCheck?.hasFocusVisible) {
            warnings.push('no :focus-visible CSS rules detected');
        }
        if (metrics.focusVisibleCheck?.noVisibleFocus?.length) {
            warnings.push(`elements without visible focus indicator: ${metrics.focusVisibleCheck.noVisibleFocus.length}`);
        }
        if (metrics.unguardedAnimations?.unguarded?.length > 5) {
            warnings.push(`animations without prefers-reduced-motion guard: ${metrics.unguardedAnimations.unguarded.length}`);
        }
        if (metrics.clippedDropdowns?.length) {
            warnings.push(`dropdowns inside overflow:hidden containers: ${metrics.clippedDropdowns.length}`);
        }
        if (metrics.importantAbuse?.count > 10) {
            warnings.push(`excessive !important usage: ${metrics.importantAbuse.count}`);
        }
        if (metrics.tightlyPackedTargets?.length) {
            warnings.push(`tightly packed touch targets: ${metrics.tightlyPackedTargets.length}`);
        }

        failures.push(...sameOriginFailures);

        if (traffic.pageErrors.length) {
            failures.push(...traffic.pageErrors.map((entry) => `pageerror ${entry}`));
        }
        if (traffic.consoleEntries.length) {
            warnings.push(...traffic.consoleEntries.map((entry) => `console ${entry.type}: ${entry.text}`));
        }
        warnings.push(...externalWarnings);

        return {
            name: view.name,
            url: page.url(),
            failures,
            warnings,
            metrics: {
                duplicateIds: metrics.duplicateIds.length,
                unlabeledControls: metrics.unlabeledControls.length,
                missingSelectors: metrics.missingSelectors.length,
                missingSelectorList: metrics.missingSelectors,
                horizontalOverflow: metrics.horizontalOverflow,
                overflowAmount: metrics.overflowAmount,
                smallTouchTargets: metrics.smallTouchTargets.length,
                tightlyPackedTargets: metrics.tightlyPackedTargets?.length || 0,
                contrastIssues: metrics.contrastIssues.length,
                layoutDriftIssues: metrics.layoutDriftIssues.length,
                unstyledDisabledControls: metrics.unstyledDisabledControls?.length || 0,
                disabledWithoutStyle: metrics.disabledWithoutStyle?.length || 0,
                badInputs: metrics.badInputs.length,
                inconsistentButtons: metrics.inconsistentButtons,
                tinyText: metrics.tinyText.length,
                weakText: metrics.weakText.length,
                spacingIssues: metrics.spacingIssues.length,
                alignmentIssues: metrics.alignmentIssues.length,
                hardcodedColors: metrics.hardcodedColors.length,
                badIconButtons: metrics.badIconButtons.length,
                tokenViolations: metrics.tokenViolations.length,
                iconButtonsWithoutAria: metrics.iconButtonsWithoutAria.length,
                unlabeledInputsStrict: metrics.unlabeledInputsStrict.length,
                legacyClassViolations: metrics.legacyClassViolations,
                hasMainLandmark: metrics.hasMainLandmark,
                hasAppShell: metrics.hasAppShell,
                insufficientFixedTopOffset: metrics.insufficientFixedTopOffset,
                footerNotFlex: metrics.footerNotFlex,
                mutationObservers: metrics.mutationObservers,
                duplicateEventHandlers: metrics.duplicateEventHandlers,
                visualDriftRatio: visual.ratio,
                // New CSS review metrics
                undefinedCustomProperties: metrics.undefinedCustomProperties?.length || 0,
                backdropFilterCount: metrics.backdropFilterCount || 0,
                stickyWithoutBackground: metrics.stickyWithoutBackground?.length || 0,
                hasFocusVisibleRules: metrics.focusVisibleCheck?.hasFocusVisible ?? false,
                noVisibleFocusIndicators: metrics.focusVisibleCheck?.noVisibleFocus?.length || 0,
                unguardedAnimations: metrics.unguardedAnimations?.unguarded?.length || 0,
                clippedDropdowns: metrics.clippedDropdowns?.length || 0,
                importantAbuse: metrics.importantAbuse?.count || 0,
                fontLoadingStatus: metrics.fontLoadingStatus?.status || 'unknown',
            },
            screenshots: {
                first: shots.shotA,
                second: shots.shotB,
                diff: visual.diffPath,
            },
        };
    } finally {
        await context.close();
    }
}

/**
 * Entry point: sets up output directories, launches a single browser instance,
 * runs all views in parallel, writes results.json, and sets process.exitCode.
 */
async function main() {
    ensureDir(OUTPUT_DIR);
    ensureDir(SCREENSHOT_DIR);

    const browser = await chromium.launch({ headless: true });

    try {
        const loginRequired = await detectLoginRequired();
        const authState = await createAuthState(browser);
        const firstJobId = process.env.UI_LINT_JOB_ID || await discoverJobId(browser, authState);
        const results = [];

        results.push(await runInvalidLoginCheck(browser, loginRequired));

        const concurrency = Math.max(1, envInt('UI_LINT_CONCURRENCY', 2));
        const queue = VIEW_DEFS.map((view, index) => ({ view, index }));
        const parallelResults = new Array(VIEW_DEFS.length);
        const workers = Array.from({ length: concurrency }, async () => {
            while (queue.length) {
                const item = queue.shift();
                if (!item) break;
                if (item.view.name === 'login' && !loginRequired) {
                    parallelResults[item.index] = buildSkippedResult(
                        item.view.name,
                        `${BASE_URL}${item.view.url}`,
                        'login is disabled; skipped login page audit',
                    );
                    continue;
                }
                parallelResults[item.index] = await runViewSafely(browser, authState, item.view);
            }
        });
        await Promise.all(workers);
        results.push(...parallelResults.filter(Boolean));

        if (firstJobId) {
            results.push(await runViewSafely(browser, authState, {
                name: 'job-detail',
                url: '/job/:jobId',
                readySelector: '#status',
                auth: true,
                device: 'desktop',
                requiredSelectors: ['#status', '#message'],
            }, { jobId: firstJobId }));
        }

        const totals = results.reduce((acc, result) => {
            acc.failures += result.failures.length;
            acc.warnings += result.warnings.length;
            return acc;
        }, { failures: 0, warnings: 0 });

        const payload = {
            baseUrl: BASE_URL,
            outputDir: OUTPUT_DIR,
            generatedAt: new Date().toISOString(),
            results,
            totals,
        };

        await writeFile(RESULTS_PATH, JSON.stringify(payload, null, 2));

        console.log('UI_LINT_START');
        console.log(`Output: ${OUTPUT_DIR}`);
        for (const result of results) {
            const status = result.failures.length ? 'FAIL' : 'PASS';
            console.log(`${status} ${result.name} ${formatResultSummary(result)}`.trim());
            for (const failure of result.failures) {
                console.log(`  hard: ${failure}`);
            }
            for (const warning of result.warnings) {
                console.log(`  warn: ${warning}`);
            }
        }
        console.log(`Totals: failures=${totals.failures} warnings=${totals.warnings}`);
        console.log(`Results JSON: ${RESULTS_PATH}`);

        process.exitCode = totals.failures > 0 ? 1 : 0;
    } finally {
        // Isolate cleanup errors so they never shadow the original exception.
        try {
            await browser.close();
        } catch (closeErr) {
            console.error('Browser cleanup failed:', closeErr);
        }
    }
}

main().catch((error) => {
    console.error(error instanceof Error ? error.stack || error.message : String(error));
    process.exit(1);
});
