//
// tools/ui-lint/run-ui-lint.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { chromium, devices, firefox, webkit } from 'playwright';

import { runAxeAudit } from './lib/axe.mjs';
import {
    collectConsoleAndNetwork,
    diffScreenshots,
    disableMotion,
    ensureDir,
    login,
    sanitize,
} from './lib/browser-utils.mjs';
import { triageConsoleEntries } from './lib/console-severity.mjs';
import {
    classifyLayoutShift,
    collectLayoutShift,
    installLayoutShiftObserver,
    LAYOUT_SHIFT_POOR,
} from './lib/layout-shift.mjs';
import { buildUIHealthReport, summarizeHealthReports } from './lib/ui-health.mjs';

const BASE_URL = process.env.UI_LINT_BASE_URL || 'http://127.0.0.1:8000';
const BASE_ORIGIN = new URL(BASE_URL).origin;
const USERNAME = process.env.UI_LINT_USERNAME;
const PASSWORD = process.env.UI_LINT_PASSWORD;
const SESSION_ID = Date.now();
const OUTPUT_DIR = process.env.UI_LINT_OUTPUT_DIR || `/tmp/fetchly-ui-lint-${SESSION_ID}`;
const SCREENSHOT_DIR = path.join(OUTPUT_DIR, 'screenshots');
const RESULTS_PATH = path.join(OUTPUT_DIR, 'results.json');
const MAIN_JS_PATH = new URL('../../app/static/js/main.js', import.meta.url);
const JOBS_JS_PATH = new URL('../../app/static/js/jobs.js', import.meta.url);
const UI_JS_PATH = new URL('../../app/static/js/ui.js', import.meta.url);
const SETTINGS_JS_PATH = new URL('../../app/static/js/settings.js', import.meta.url);
const SETTINGS_TEMPLATE_PATH = new URL('../../app/templates/settings.html', import.meta.url);
const SETTINGS_STYLE_PATH = new URL('../../app/static/style.css', import.meta.url);

let jobsSourceContractMetricsPromise;
let settingsSourceContractMetricsPromise;

function viewAuditsJobsList(view) {
    return Array.isArray(view?.requiredSelectors) && view.requiredSelectors.includes('#jobsRenderRoot');
}

function viewAuditsSettings(view) {
    return Array.isArray(view?.requiredSelectors) && view.requiredSelectors.includes('#settingsForm');
}

async function getJobsSourceContractMetrics() {
    if (!jobsSourceContractMetricsPromise) {
        jobsSourceContractMetricsPromise = (async () => {
            const [mainJsSource, jobsJsSource, uiJsSource] = await Promise.all([
                readFile(MAIN_JS_PATH, 'utf8'),
                readFile(JOBS_JS_PATH, 'utf8'),
                readFile(UI_JS_PATH, 'utf8'),
            ]);

            const observerBoundToScroller = /new IntersectionObserver\([\s\S]*?root:\s*jobsScrollContainer[\s\S]*?observer\.observe\(jobsSentinel\)/.test(mainJsSource);
            const localScrollFallback = /jobsScrollContainer\.addEventListener\(\s*['"]scroll['"]\s*,\s*onScroll/.test(mainJsSource);
            const windowScrollLoadsMore = /window\.addEventListener\(\s*['"]scroll['"][\s\S]{0,400}?maybeLoadMoreJobs/.test(mainJsSource);

            const storeLengthAssignments = [...jobsJsSource.matchAll(/state\.nextOffset\s*=\s*state\.jobs\.length/g)].length;
            const initSeedsFromStoreLength = /function init\(\)\s*\{[\s\S]*?state\.nextOffset\s*=\s*state\.jobs\.length/.test(jobsJsSource);
            const usesLegacyDomSyncHelper = /function\s+syncNextOffset\s*\(/.test(jobsJsSource)
                || /\bsyncNextOffset\(\)/.test(jobsJsSource);

            const tracksMonotonicOffset = /nextOffset:\s*0/.test(jobsJsSource)
                && /const currentOffset = state\.nextOffset/.test(jobsJsSource)
                && /await fetchFn\(currentOffset\)/.test(jobsJsSource)
                && /state\.nextOffset\s*=\s*currentOffset\s*\+\s*jobs\.length/.test(jobsJsSource);
            const usesRenderedCountAsFetchOffset = /fetchFn\(\s*(?:getRenderedJobCount\(|tbody\s*\.\s*querySelectorAll\(|document\s*\.\s*querySelectorAll\()[\s\S]*?\)/.test(jobsJsSource);
            const hasUnexpectedStoreLengthResync = storeLengthAssignments > (initSeedsFromStoreLength ? 1 : 0);

            const desktopMediaSecondarySource = jobsJsSource.match(
                /function formatDesktopMediaSecondary\(job\)\s*\{([\s\S]*?)\n\}/,
            )?.[1] || '';
            const desktopMediaDetailUsesSharedText = (
                /humanSize\(job\?\.filesize_bytes\)/.test(desktopMediaSecondarySource)
                && /mediaDetail\.className\s*=\s*["']meta-sub job-media-detail["'];[\s\S]{0,200}?mediaDetail\.textContent\s*=\s*formatDesktopMediaSecondary\(job\)/.test(jobsJsSource)
            );
            const desktopStatusWithoutSizeCalls = [
                ...jobsJsSource.matchAll(/renderJobStatus\(job,\s*\{\s*showSize:\s*false\s*\}\)/g),
            ].length;
            const mobileDownloadActionSource = uiJsSource.match(
                /function createMobileDownloadAction\(job\)\s*\{([\s\S]*?)\n\}/,
            )?.[1] || '';
            const mobileSharesDesktopDownloadMenu = (
                /createDownloadOptionsMenu\(job,\s*downloadBtn\.href\)/.test(mobileDownloadActionSource)
                && /function createDownloadOptionsMenu\(job,\s*downloadHref\)[\s\S]*?["']share["'][\s\S]*?action:\s*["']share-job["']/.test(uiJsSource)
                && /return createMobileDownloadAction\(action\.job\)/.test(uiJsSource)
            );

            return {
                jobsInfiniteScrollNotObserverBased: !(observerBoundToScroller && localScrollFallback) || windowScrollLoadsMore,
                jobsPagingOffsetContractBroken: !tracksMonotonicOffset || usesRenderedCountAsFetchOffset || usesLegacyDomSyncHelper || hasUnexpectedStoreLengthResync,
                jobsDesktopFileSizePlacementBroken: !desktopMediaDetailUsesSharedText
                    || desktopStatusWithoutSizeCalls < 2,
                jobsMobileShareActionMissing: !mobileSharesDesktopDownloadMenu,
            };
        })();
    }

    return jobsSourceContractMetricsPromise;
}

async function getSettingsSourceContractMetrics() {
    if (!settingsSourceContractMetricsPromise) {
        settingsSourceContractMetricsPromise = (async () => {
            const [settingsJsSource, settingsTemplateSource, settingsStyleSource] = await Promise.all([
                readFile(SETTINGS_JS_PATH, 'utf8'),
                readFile(SETTINGS_TEMPLATE_PATH, 'utf8'),
                readFile(SETTINGS_STYLE_PATH, 'utf8'),
            ]);

            const validationUsesToast = /showToast\(\s*validation\.error\s*,\s*["']danger["']\s*\)/.test(settingsJsSource);
            const autosaveSuccessToast = /showToast\(\s*payload\.message\s*\|\|\s*["']Settings updated["']\s*,\s*["']success["']\s*\)/.test(settingsJsSource);
            const jsUsesInlineSaveStatus = /\b(?:AUTO_SAVE_STATE|setAutoSaveState|autoSaveIndicator|settingsSaveBtn|settingsAlert)\b/.test(settingsJsSource);
            const templateHasInlineSaveStatus = /(?:id=["'](?:autoSaveIndicator|settingsSaveBtn|settingsAlert)["']|class=["'][^"']*settings-status-bar)/.test(settingsTemplateSource);

            const hints = [...settingsTemplateSource.matchAll(
                /<(small|p)\b[^>]*\bclass=["'][^"']*\bsetting-hint\b[^"']*["'][^>]*>([\s\S]*?)<\/\1>/g,
            )];
            const hintContractBroken = hints.length === 0 || hints.some((hint) => {
                const openingTag = hint[0].slice(0, hint[0].indexOf('>') + 1);
                const content = hint[2];
                return !openingTag.includes('setting-hint--with-icon')
                    || !/^\s*<span\b[^>]*\bclass=["'][^"']*\bmaterial-symbols-outlined\b[^"']*\bsetting-hint-icon\b[^"']*["'][^>]*>\s*info\s*<\/span>/.test(content);
            });
            const hintStyleContractBroken = !(
                /--text-hint\s*:/.test(settingsStyleSource)
                && /\.app-root--settings\s+\.setting-hint\s*\{[\s\S]*?color:\s*var\(--text-hint\)/.test(settingsStyleSource)
                && /\.app-root--settings\s+\.setting-hint--with-icon\s*\{[\s\S]*?display:\s*inline-flex/.test(settingsStyleSource)
            );
            const settingsStyleRules = settingsStyleSource.replace(/\/\*[\s\S]*?\*\//g, '');
            const hintMarginRules = [...settingsStyleRules.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(
                ([, selector, declarations]) => /(^|[\s>+~,])\.setting-hint(?![-\w])/.test(selector)
                    && /margin-top\s*:/.test(declarations),
            );
            const settingsHintSpacingContractBroken = hintMarginRules.length !== 1
                || !/\.app-root--settings\s+\.setting-hint\s*$/.test(hintMarginRules[0]?.[1] || '')
                || !/margin-top\s*:\s*var\(--space-1\)/.test(hintMarginRules[0]?.[2] || '');

            return {
                settingsSaveToastContractBroken: !validationUsesToast
                    || autosaveSuccessToast
                    || jsUsesInlineSaveStatus
                    || templateHasInlineSaveStatus,
                settingsHintContractBroken: hintContractBroken || hintStyleContractBroken,
                settingsHintSpacingContractBroken,
            };
        })();
    }

    return settingsSourceContractMetricsPromise;
}

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
const MOBILE_TOUCH_TARGET_MIN = envInt('UI_LINT_TOUCH_TARGET_MIN', 44);
const DESKTOP_TOUCH_TARGET_MIN = envInt('UI_LINT_DESKTOP_TOUCH_TARGET_MIN', 32);
const SCREENSHOT_SETTLE_MS = envInt('UI_LINT_SCREENSHOT_SETTLE_MS', 500);
const HARDCODED_COLOR_FAIL_THRESHOLD = envInt('UI_LINT_HARDCODED_COLOR_FAIL_THRESHOLD', 40);
const TOKEN_VIOLATION_FAIL_THRESHOLD = envInt('UI_LINT_TOKEN_VIOLATION_FAIL_THRESHOLD', 0);
const LAYOUT_STABLE_EPSILON_PX = envInt('UI_LINT_LAYOUT_STABLE_EPSILON_PX', 1);
const MOBILE_LAYOUT_STABLE_FRAMES = envInt('UI_LINT_MOBILE_LAYOUT_STABLE_FRAMES', 4);
const DESKTOP_LAYOUT_STABLE_FRAMES = envInt('UI_LINT_DESKTOP_LAYOUT_STABLE_FRAMES', 2);
const MOBILE_LAYOUT_MAX_FRAMES = envInt('UI_LINT_MOBILE_LAYOUT_MAX_FRAMES', 36);
const DESKTOP_LAYOUT_MAX_FRAMES = envInt('UI_LINT_DESKTOP_LAYOUT_MAX_FRAMES', 16);

/**
 * Reads a comma-separated allowlist, falling back to the full set. Unknown
 * names fail loudly: a typo in UI_LINT_BROWSERS would otherwise silently
 * narrow the audit to nothing.
 * @param {string} name
 * @param {string[]} allowed
 * @returns {string[]}
 */
function envEngineList(name, allowed) {
    const raw = process.env[name];
    if (raw === undefined || raw.trim() === '') return [...allowed];

    const selected = raw.split(',').map((entry) => entry.trim().toLowerCase()).filter(Boolean);
    const unknown = selected.filter((entry) => !allowed.includes(entry));
    if (unknown.length) {
        throw new Error(`${name} names unknown engines: ${unknown.join(', ')}. Known: ${allowed.join(', ')}`);
    }
    if (!selected.length) {
        throw new Error(`${name} selected no engines`);
    }
    return selected;
}

/** Playwright launchers, keyed by the engine name a device profile declares. */
const ENGINE_LAUNCHERS = Object.freeze({ chromium, webkit, firefox });

const SELECTED_ENGINES = envEngineList('UI_LINT_BROWSERS', Object.keys(ENGINE_LAUNCHERS));
// Engines are separate browser processes, so they overlap without contending
// for the same page pool; devices within one engine share it and need their
// own limit. UI_LINT_CONCURRENCY is kept as the default for the device limit
// so existing recipes (setup.conf) keep their meaning.
const BROWSER_CONCURRENCY = Math.max(1, envInt('UI_LINT_BROWSER_CONCURRENCY', SELECTED_ENGINES.length));
const DEVICE_CONCURRENCY = Math.max(1, envInt('UI_LINT_DEVICE_CONCURRENCY', envInt('UI_LINT_CONCURRENCY', 2)));
const RUN_AXE = envInt('UI_LINT_AXE', 1) !== 0;
// Which axe impact level becomes a hard failure. 'critical' matches how the
// runner already treats its own accessibility checks - icon buttons without an
// accessible name and unlabeled inputs are failures, not warnings - so the
// imported ruleset is held to the same bar. Set to 'none' to land the ruleset
// report-only while the existing violations are worked off.
const AXE_FAIL_ON = (() => {
    const raw = (process.env.UI_LINT_AXE_FAIL_ON || 'critical').trim().toLowerCase();
    if (!['critical', 'serious', 'none'].includes(raw)) {
        throw new Error(`UI_LINT_AXE_FAIL_ON must be one of critical, serious, none - got ${raw}`);
    }
    return raw;
})();
// Report-only by default. The score is new and uncalibrated against this
// codebase; turning it into a gate before the baseline is known would only
// teach everyone to set it back to 0. Raise it once the run is clean.
const HEALTH_MIN = envInt('UI_LINT_HEALTH_MIN', 0);
// The hard block is the score's categorical half: a critical UX issue or a
// blocking axe violation, regardless of how good the rest of the view is. It
// is reported either way; this turns it into a failure. Off by default for the
// same reason as UI_LINT_HEALTH_MIN - it currently fires on findings the
// runner has always treated as warnings (undersized touch targets), and
// promoting those is a decision, not a side effect of adding the score.
const HEALTH_GATE = envInt('UI_LINT_HEALTH_GATE', 0) !== 0;
const PLATFORM_BADGE_WIDTH_PX = 28;
const PLATFORM_BADGE_HEIGHT_PX = 24;
const PLATFORM_BADGE_ICON_SIZE_PX = 16;
const PLATFORM_BADGE_GEOMETRY_EPSILON_PX = 0.1;
const MOTION_RESET_CSS = `
  *, *::before, *::after {
    animation: none !important;
    transition: none !important;
    scroll-behavior: auto !important;
  }
`;
// Media stays visible during screenshots. Hiding it changes the DOM under test
// and makes the invisible-media check report the linter's own injected CSS.
const VISUAL_STABILITY_CSS = '';
// Deliberately empty. The previous sheet forced -webkit-text-size-adjust,
// overscroll-behavior and scrollbar-gutter on every mobile view, which are the
// exact behaviours an iOS audit exists to observe: the gutter reserves width
// that iOS (overlay scrollbars) never reserves, so every mobile measurement was
// taken against a layout the device does not produce. Screenshot stability is
// already handled by MOTION_RESET_CSS plus the layout-settle wait.
const MOBILE_VISUAL_STABILITY_CSS = '';

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
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsRenderRoot', '#jobsTable'],
    },
    {
        name: 'settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'desktop',
        requiredSelectors: ['#settingsForm', '#lalalAuthBtn'],
    },
    {
        name: 'mobile-login',
        url: '/login',
        readySelector: '#login-form',
        auth: false,
        device: 'mobile',
        requiredSelectors: ['#username', '#password', '#submit-btn'],
    },
    {
        name: 'mobile-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'mobile',
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsRenderRoot', '#jobsMobileList', '#showJobHistoryToggle'],
    },
    {
        name: 'mobile-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'mobile',
        requiredSelectors: ['#settingsForm', '#lalalAuthBtn'],
    },
    // iPad portrait (768px): the narrow edge of the tablet band. The desktop
    // table renders here, so the phone-only selectors are deliberately absent
    // from requiredSelectors.
    {
        name: 'tablet-login',
        url: '/login',
        readySelector: '#login-form',
        auth: false,
        device: 'tablet',
        requiredSelectors: ['#username', '#password', '#submit-btn'],
    },
    {
        name: 'tablet-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'tablet',
        // 768px is inside `@media (max-width: 1024px)`, so the feed renders
        // here exactly as it does on the phone.
        requiredSelectors: [
            '.stats-row',
            '#submitForm',
            '#jobsRenderRoot',
            '#jobsMobileList',
            '#showJobHistoryToggle',
        ],
    },
    {
        name: 'tablet-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'tablet',
        requiredSelectors: ['#settingsForm', '#lalalAuthBtn'],
    },
    // iPad landscape (1024px): the wide edge, still inside (max-width: 1024px).
    {
        name: 'tablet-landscape-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'tablet-landscape',
        requiredSelectors: [
            '.stats-row',
            '#submitForm',
            '#jobsRenderRoot',
            '#jobsMobileList',
            '#showJobHistoryToggle',
        ],
    },
    {
        name: 'tablet-landscape-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'tablet-landscape',
        requiredSelectors: ['#settingsForm', '#lalalAuthBtn'],
    },
    // iPad Pro 11 landscape (1194px): past the compact breakpoint, so the
    // desktop table renders - on a touch screen.
    {
        name: 'tablet-wide-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'tablet-wide',
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsRenderRoot', '#jobsTable'],
    },
    {
        name: 'tablet-wide-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'tablet-wide',
        requiredSelectors: ['#settingsForm', '#lalalAuthBtn'],
    },
    // Gecko at the desktop viewport. Same three routes as `desktop`, so any
    // finding that appears here and not there is an engine difference rather
    // than a layout one.
    {
        name: 'firefox-login',
        url: '/login',
        readySelector: '#login-form',
        auth: false,
        device: 'desktop-firefox',
        requiredSelectors: ['#username', '#password', '#submit-btn'],
    },
    {
        name: 'firefox-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'desktop-firefox',
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsRenderRoot', '#jobsTable'],
    },
    {
        name: 'firefox-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'desktop-firefox',
        requiredSelectors: ['#settingsForm', '#lalalAuthBtn'],
    },
];

// Every device the invalid-login check runs on needs its own /login view;
// falling back to the desktop one would audit the wrong viewport in silence.
for (const required of ['desktop', 'mobile', 'tablet']) {
    if (!VIEW_DEFS.some((view) => view.url === '/login' && view.device === required)) {
        throw new Error(`VIEW_DEFS must contain a /login view for device '${required}'`);
    }
}

/**
 * Returns the login view definition for a device, so the invalid-login check
 * runs at that device's viewport instead of silently reusing the desktop one.
 * @param {string} device
 * @returns {object}
 */
function loginViewFor(device) {
    const view = VIEW_DEFS.find((candidate) => candidate.url === '/login' && candidate.device === device);
    if (!view) {
        throw new Error(`VIEW_DEFS has no /login view for device '${device}'`);
    }
    return view;
}

// Views whose name is the desktop variant plus a mobile counterpart. Audits
// gated on a single view name must accept both, otherwise the WebKit run
// silently loses coverage the desktop run has.
// Derived from VIEW_DEFS rather than restated: these lists gate whole audits
// (the settings tab-gap check, the platform-badge geometry check), and a view
// added to VIEW_DEFS but forgotten in a hand-written list is skipped silently
// - it still reports PASS, just without ever having run the audit.
const viewNamesForUrl = (url) => VIEW_DEFS.filter((view) => view.url === url).map((view) => view.name);

const LOGIN_VIEW_NAMES = viewNamesForUrl('/login');
const DASHBOARD_VIEW_NAMES = viewNamesForUrl('/');
const SETTINGS_VIEW_NAMES = viewNamesForUrl('/settings');
// The job-detail views are built at runtime from a discovered job id, so they
// are not in VIEW_DEFS and stay listed explicitly.
const JOB_DETAIL_VIEW_NAMES = ['job-detail', 'mobile-job-detail', 'tablet-job-detail'];

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

/**
 * Device profiles the audit runs against.
 *
 * Form factor and touch are deliberately separate axes. A tablet is touch but
 * renders the desktop table, so a single isMobile flag cannot describe it:
 * gating touch-target size on "is this the phone layout" would hand the iPad
 * the 32px desktop minimum, and gating the phone DOM contracts on "does this
 * have touch" would demand `#jobsMobileList` on a viewport that never renders
 * it. `formFactor` answers the layout question, `hasTouch` the input one.
 *
 * The viewport widths matter: app/static/style.css carries a tablet band -
 * `(min-width: 768px) and (max-width: 1024px)`, `(max-width: 1024px)`,
 * `(min-width: 576.02px) and (max-width: 1024px)` - that neither the 390px
 * phone nor the 1440px desktop context ever renders.
 */
const DEVICE_PROFILES = {
    desktop: {
        engine: 'chromium',
        formFactor: 'desktop',
        playwrightDevice: null,
        viewport: { width: 1440, height: 1200 },
    },
    mobile: {
        engine: 'webkit',
        formFactor: 'phone',
        playwrightDevice: 'iPhone 13',
    },
    tablet: {
        engine: 'webkit',
        formFactor: 'tablet',
        // 768x1024: inside the compact band, so the jobs feed renders.
        playwrightDevice: 'iPad Mini',
    },
    'tablet-landscape': {
        engine: 'webkit',
        formFactor: 'tablet',
        // 1024x768: the exact boundary of `(max-width: 1024px)`. Off-by-one
        // breakpoint mistakes surface here and nowhere else.
        playwrightDevice: 'iPad Mini landscape',
    },
    'tablet-wide': {
        engine: 'webkit',
        formFactor: 'tablet',
        // 1194x834: past the breakpoint, so the desktop table renders on a
        // touch screen. This is the case a single isMobile flag cannot express
        // - desktop layout, finger-sized hit areas.
        playwrightDevice: 'iPad Pro 11 landscape',
    },
    'desktop-firefox': {
        engine: 'firefox',
        formFactor: 'desktop',
        // Same viewport as `desktop` on purpose: this profile exists to vary
        // the engine, not the layout. Gecko resolves flexbox min-size,
        // scrollbar gutters and subgrid differently from Blink, and those
        // differences only show when everything else is held constant.
        playwrightDevice: null,
        viewport: { width: 1440, height: 1200 },
    },
};

// The breakpoint app/static/style.css uses to swap the desktop jobs table for
// the mobile feed: `@media (max-width: 1024px)`. The audit derives "compact
// layout" from the viewport rather than from the device name, so a new profile
// lands on the right side of the contract automatically.
const COMPACT_LAYOUT_MAX_WIDTH = 1024;

/**
 * True when the profile's viewport renders the compact (feed) layout.
 * @param {string} device
 * @returns {boolean}
 */
function profileIsCompactLayout(device) {
    const { viewport } = createContextOptions(device);
    return Number(viewport?.width) <= COMPACT_LAYOUT_MAX_WIDTH;
}

/**
 * Returns the profile for a view's device, failing loudly on a typo rather
 * than silently auditing the desktop layout.
 * @param {string} device
 * @returns {object}
 */
function deviceProfile(device) {
    const profile = DEVICE_PROFILES[device];
    if (!profile) {
        throw new Error(`Unknown device '${device}'. Known: ${Object.keys(DEVICE_PROFILES).join(', ')}`);
    }
    return profile;
}

/** True when the profile emulates a touch screen (phone or tablet). */
function profileHasTouch(device) {
    return deviceProfile(device).formFactor !== 'desktop';
}

function createContextOptions(device) {
    const profile = deviceProfile(device);

    // The application CSP correctly blocks inline styles. The lint runner
    // injects its own stability stylesheet, so bypass CSP only in this
    // isolated test context.
    const shared = { colorScheme: 'dark', locale: 'en-US', bypassCSP: true };

    if (!profile.playwrightDevice) {
        return {
            ...shared,
            viewport: { ...profile.viewport },
            screen: { ...profile.viewport },
            deviceScaleFactor: 1,
            hasTouch: false,
        };
    }

    const descriptor = devices[profile.playwrightDevice];
    if (!descriptor) {
        throw new Error(`Playwright has no device descriptor named '${profile.playwrightDevice}'`);
    }

    return {
        ...descriptor,
        viewport: { ...descriptor.viewport },
        screen: { ...(descriptor.screen || descriptor.viewport) },
        ...shared,
    };
}

/**
 * Formats a result object into a compact single-line summary string for console output.
 * @param {object} result
 * @returns {string}
 */
function formatResultSummary(result) {
    const parts = [];
    // Health first: it is the one number that says how bad the view is, and
    // the counters below only explain it.
    if (result.health) parts.push(`health=${result.health.score}/${result.health.severity}`);
    if (result.failures.length) parts.push(`failures=${result.failures.length}`);
    if (result.warnings.length) parts.push(`warnings=${result.warnings.length}`);
    const metrics = result.metrics || {};
    // A view that never ran says so once, on the status line; repeating it per
    // audit would only pad the summary.
    if (!metrics.skipped) {
        if (metrics.axeAvailable === false) parts.push('axe=skipped');
        else if (metrics.axeCritical || metrics.axeSerious) {
            parts.push(`axe=${metrics.axeCritical}c/${metrics.axeSerious}s`);
        }
    }
    if (metrics.layoutShiftSupported && metrics.layoutShiftValue > 0) {
        parts.push(`cls=${metrics.layoutShiftValue}`);
    }
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
    if (metrics.footerSpaceReservationMissing) parts.push('footerSpaceReservation=bad');
    if (metrics.footerViewportPlacementBroken) parts.push('footerViewportPlacement=bad');
    if (metrics.footerHistoryTransitionBroken) parts.push('footerHistoryTransition=bad');
    if (metrics.mutationObservers) parts.push(`mutationObservers=${metrics.mutationObservers}`);
    if (metrics.duplicateEventHandlers) parts.push(`duplicateGlobalClicks=${metrics.duplicateEventHandlers}`);
    if (metrics.visualDriftRatio > 0) parts.push(`visualDrift=${metrics.visualDriftRatio.toFixed(5)}`);
    if (metrics.trimInvalidRanges) parts.push('trimInvalidRange=true');
    if (metrics.trimSnapViolations) parts.push(`trimSnap=${metrics.trimSnapViolations}`);
    if (metrics.trimLoopDriftMs) parts.push(`trimLoopDrift=${metrics.trimLoopDriftMs}`);
    if (metrics.trimZoomInstability) parts.push(`trimZoom=${metrics.trimZoomInstability}`);
    if (metrics.trimHandleTooSmall) parts.push(`trimHandles=${metrics.trimHandleTooSmall}`);
    if (metrics.trimKeyboardMissing?.length) parts.push(`trimKeysMissing=${metrics.trimKeyboardMissing.join('|')}`);
    if (metrics.trimKeyboardConflicts?.length) parts.push(`trimKeyConflict=${metrics.trimKeyboardConflicts.join('|')}`);
    if (metrics.trimInstanceLeaks) parts.push(`trimLeaks=${metrics.trimInstanceLeaks}`);
    if (metrics.trimUiAudioMismatchMs) parts.push(`trimDrift=${metrics.trimUiAudioMismatchMs}`);
    // New checks from CSS review
    if (metrics.undefinedCustomProperties) parts.push(`undefinedVars=${metrics.undefinedCustomProperties}`);
    if (metrics.backdropFilterCount > 4) parts.push(`backdropFilters=${metrics.backdropFilterCount}`);
    if (metrics.stickyWithoutBackground) parts.push(`stickyNoBackground=${metrics.stickyWithoutBackground}`);
    if (!metrics.hasFocusVisibleRules) parts.push('noFocusVisible=true');
    if (metrics.noVisibleFocusIndicators) parts.push(`noFocusRing=${metrics.noVisibleFocusIndicators}`);
    if (metrics.unguardedAnimations) parts.push(`unguardedAnimations=${metrics.unguardedAnimations}`);
    if (metrics.clippedDropdowns) parts.push(`clippedDropdowns=${metrics.clippedDropdowns}`);
    if (metrics.jobsActionRightEdgeSpacing) parts.push(`jobsActionRightEdgeSpacing=${metrics.jobsActionRightEdgeSpacing}`);
    if (metrics.escapedHtmlElements) parts.push(`escapedHtml=${metrics.escapedHtmlElements}`);
    if (metrics.importantAbuse) parts.push(`importantAbuse=${metrics.importantAbuse}`);
    if (metrics.tightlyPackedTargets) parts.push(`tightTargets=${metrics.tightlyPackedTargets}`);
    if (metrics.localOverflowIssues) parts.push(`localOverflow=${metrics.localOverflowIssues}`);
    if (metrics.brokenTitleTruncation) parts.push(`brokenEllipsis=${metrics.brokenTitleTruncation}`);
    if (metrics.mobileJobsFeedIssues) parts.push(`mobileJobsFeed=${metrics.mobileJobsFeedIssues}`);
    if (metrics.tabletLayoutIssues) parts.push(`tabletLayout=${metrics.tabletLayoutIssues}`);
    if (metrics.mixedLayoutIssues) parts.push(`mixedLayout=${metrics.mixedLayoutIssues}`);
    if (metrics.statCardCenteringIssues) parts.push(`statCardCentering=${metrics.statCardCenteringIssues}`);
    if (metrics.containerWidthIssue) parts.push('containerWidth=bad');
    if (metrics.statTileMobileLayoutIssues) parts.push(`statTileMobileLayout=${metrics.statTileMobileLayoutIssues}`);
    if (metrics.focusIndicatorMissing) parts.push(`focusIndicatorMissing=${metrics.focusIndicatorMissing}`);
    if (metrics.ghostScrollContainers) parts.push(`ghostScroll=${metrics.ghostScrollContainers}`);
    if (metrics.nestedScrollContainers) parts.push(`nestedScroll=${metrics.nestedScrollContainers}`);
    if (metrics.flexScrollTraps) parts.push(`flexScrollTraps=${metrics.flexScrollTraps}`);
    if (metrics.doubleScrollRisk) parts.push(`doubleScrollRisk=${metrics.doubleScrollRisk}`);
    if (metrics.viewportScrollLeak) parts.push(`viewportScrollLeak=${metrics.viewportScrollLeak}`);
    if (metrics.overflowHiddenScrollBlockers > 0)
        parts.push(`overflowHiddenBlocks=${metrics.overflowHiddenScrollBlockers}`);
    if (metrics.hasSelectorLayoutUsage > 0)
        parts.push(`hasLayoutRules=${metrics.hasSelectorLayoutUsage}`);
    if (metrics.viewportLockingIssues > 0)
        parts.push(`viewportLock=${metrics.viewportLockingIssues}`);
    if (metrics.badgeInconsistencies) parts.push(`badgeInconsistencies=${metrics.badgeInconsistencies}`);
    if (metrics.platformBadgeGeometryIssues) {
        parts.push(`platformBadgeGeometry=${metrics.platformBadgeGeometryIssues}`);
    }
    if (metrics.iconPointerEventsIssues) parts.push(`iconPointerEvents=${metrics.iconPointerEventsIssues}`);
    if (metrics.gridViolations) parts.push(`gridViolations=${metrics.gridViolations}`);
    if (metrics.overlapIssues) parts.push(`overlapIssues=${metrics.overlapIssues}`);
    if (metrics.navbarButtonAlignment) parts.push(`navbarButtonAlignment=bad`);
    if (metrics.externalFontRequests > 0) parts.push(`externalFontRequests=${metrics.externalFontRequests}`);
    if (metrics.materialSymbolsMissingVariationSettings) parts.push('materialSymbolsNoVariationSettings=true');
    if (metrics.dropdownCaretIssues > 0) parts.push(`dropdownCaretIssues=${metrics.dropdownCaretIssues}`);
    if (metrics.brokenImages > 0) parts.push(`brokenImages=${metrics.brokenImages}`);
    if (metrics.backgroundAttachmentFixedWithoutFallback) parts.push('bgAttachmentFixedNoIOSFallback=true');
    if (metrics.loginShellAlignmentIssue) parts.push('loginShellAlignment=bad');
    // Layout stability checks
    if (metrics.submitCardNotShrinking) parts.push('submitCardNotShrinking=true');
    if (metrics.jobsCardHeaderExpanding) parts.push('jobsCardHeaderExpanding=true');
    if (metrics.tableResponsiveGhostScroll) parts.push('tableResponsiveGhostScroll=true');
    if (metrics.dashboardViewportContractBroken) parts.push('dashboardViewportContractBroken=true');
    if (metrics.jobsCardNotFillingHeight) parts.push('jobsCardNotFillingHeight=true');
    if (metrics.stickyFooterDetached) parts.push('stickyFooterDetached=true');
    if (metrics.stickyTableHeaderBroken) parts.push('stickyTableHeaderBroken=true');
    if (metrics.jobsSentinelOutsideScrollContainer) parts.push('jobsSentinelOutsideScroller=true');
    if (metrics.jobsInfiniteScrollNotObserverBased) parts.push('jobsInfiniteScrollNotObserverBased=true');
    if (metrics.jobsPagingOffsetContractBroken) parts.push('jobsPagingOffsetContractBroken=true');
    if (metrics.jobsDesktopFileSizePlacementBroken) parts.push('jobsDesktopFileSizePlacementBroken=true');
    if (metrics.jobsMobileShareActionMissing) parts.push('jobsMobileShareActionMissing=true');
    if (metrics.trimWaveformMissingStyle) parts.push('trimWaveformMissingStyle=true');
    if (metrics.videoPreviewContractBroken) parts.push('videoPreviewContractBroken=true');
    if (metrics.settingsFieldStackContractBroken) parts.push('settingsFieldStackContractBroken=true');
    if (metrics.settingsSaveToastContractBroken) parts.push('settingsSaveToastContractBroken=true');
    if (metrics.settingsHintContractBroken) parts.push('settingsHintContractBroken=true');
    if (metrics.settingsHintSpacingContractBroken) parts.push('settingsHintSpacingContractBroken=true');
    if (metrics.lalalMobileActionLayoutBroken) parts.push('lalalMobileActionLayoutBroken=true');
    if (metrics.trimDefaultSelectionInvalid) parts.push('trimDefaultSelection=invalid');
    if (metrics.uiCardChildExpands) parts.push(`uiCardChildExpands=${metrics.uiCardChildExpands}`);
    // CSS hardening (2026-04-29)
    if (metrics.webkitOnlyRules > 10) parts.push(`webkitOnlyRules=${metrics.webkitOnlyRules}`);
    if (metrics.colorMixWithoutFallback > 0) parts.push(`colorMixNoFallback=${metrics.colorMixWithoutFallback}`);
    if (metrics.willChangeAbuse > 0) parts.push(`willChangeAbuse=${metrics.willChangeAbuse}`);
    if (metrics.zIndexAbuse > 0) parts.push(`zIndexAbuse=${metrics.zIndexAbuse}`);
    if (metrics.nestedOverflowHidden > 0) parts.push(`nestedOverflowHidden=${metrics.nestedOverflowHidden}`);
    if (metrics.mobileTitleCellFlexRegression) parts.push('mobileTitleCellFlexRegression=true');
    if (metrics.mobileActionSurfaceContractBroken) parts.push('mobileActionSurfaceContractBroken=true');
    if (metrics.mobileJobsPageScrollTrap) parts.push('mobileJobsPageScrollTrap=true');
    if (metrics.emptyStateHoverHighlight) parts.push('emptyStateHoverHighlight=true');
    if (metrics.flexMinHeightOverflowHidden?.length > 0) {
        parts.push(`flexMinHeightOverflowHidden=${metrics.flexMinHeightOverflowHidden.length}`);
    }
    if (metrics.settingsTabTitleGapInconsistent) parts.push('settingsTabTitleGap=inconsistent');
    // iOS viewport hardening (2026-09-03)
    if (metrics.iosInputZoomTargets?.length) parts.push(`iosInputZoom=${metrics.iosInputZoomTargets.length}`);
    if (metrics.viewportUnitTraps?.length) parts.push(`vhWithoutDvh=${metrics.viewportUnitTraps.length}`);
    if (metrics.safeAreaInsetsDisabled) parts.push('safeAreaInsetsDisabled=true');
    if (metrics.bottomPinnedWithoutSafeArea?.length) {
        parts.push(`bottomPinnedNoSafeArea=${metrics.bottomPinnedWithoutSafeArea.length}`);
    }
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
    footerSpaceReservationMissing: false,
    footerViewportPlacementBroken: false,
    footerHistoryTransitionBroken: false,
    mutationObservers: 0,
    duplicateEventHandlers: 0,
    visualDriftRatio: 0,
    trimInvalidRanges: false,
    trimSnapViolations: 0,
    trimLoopDriftMs: 0,
    trimZoomInstability: 0,
    trimHandleTooSmall: 0,
    trimKeyboardMissing: [],
    trimKeyboardConflicts: [],
    trimInstanceLeaks: 0,
    trimUiAudioMismatchMs: 0,
    // New checks from CSS review
    undefinedCustomProperties: 0,
    backdropFilterCount: 0,
    stickyWithoutBackground: 0,
    hasFocusVisibleRules: true,
    noVisibleFocusIndicators: 0,
    unguardedAnimations: 0,
    clippedDropdowns: 0,
    jobsActionRightEdgeSpacing: 0,
    escapedHtmlElements: 0,
    importantAbuse: 0,
    localOverflowIssues: 0,
    brokenTitleTruncation: 0,
    mobileJobsFeedIssues: 0,
    tabletLayoutIssues: 0,
    mixedLayoutIssues: 0,
    containerWidthIssue: 0,
    statTileMobileLayoutIssues: 0,
    statCardCenteringIssues: 0,
    focusIndicatorMissing: 0,
    ghostScrollContainers: 0,
    nestedScrollContainers: 0,
    flexScrollTraps: 0,
    doubleScrollRisk: 0,
    viewportScrollLeak: 0,
    overflowHiddenScrollBlockers: 0,
    hasSelectorLayoutUsage: 0,
    viewportLockingIssues: 0,
    badgeInconsistencies: 0,
    platformBadgeGeometryIssues: 0,
    iconPointerEventsIssues: 0,
    gridViolations: 0,
    overlapIssues: 0,
    navbarButtonAlignment: 0,
    fontLoadingStatus: 'unknown',
    externalFontRequests: 0,
    materialSymbolsMissingVariationSettings: false,
    dropdownCaretIssues: 0,
    brokenImages: 0,
    backgroundAttachmentFixedWithoutFallback: false,
    loginShellAlignmentIssue: false,
    // Layout stability checks (2026-04-28)
    submitCardNotShrinking: false,
    jobsCardHeaderExpanding: false,
    tableResponsiveGhostScroll: false,
    dashboardViewportContractBroken: false,
    jobsCardNotFillingHeight: false,
    stickyFooterDetached: false,
    stickyTableHeaderBroken: false,
    jobsSentinelOutsideScrollContainer: false,
    jobsInfiniteScrollNotObserverBased: false,
    jobsPagingOffsetContractBroken: false,
    jobsDesktopFileSizePlacementBroken: false,
    jobsMobileShareActionMissing: false,
    trimWaveformMissingStyle: false,
    videoPreviewContractBroken: false,
    settingsFieldStackContractBroken: false,
    settingsSaveToastContractBroken: false,
    settingsHintContractBroken: false,
    settingsHintSpacingContractBroken: false,
    lalalMobileActionLayoutBroken: false,
    trimDefaultSelectionInvalid: false,
    uiCardChildExpands: 0,
    // CSS hardening checks (2026-04-29)
    webkitOnlyRules: 0,
    colorMixWithoutFallback: 0,
    willChangeAbuse: 0,
    zIndexAbuse: 0,
    nestedOverflowHidden: 0,
    mobileTitleCellFlexRegression: false,
    mobileActionSurfaceContractBroken: false,
    mobileJobsPageScrollTrap: false,
    flexMinHeightOverflowHidden: 0,
    emptyStateHoverHighlight: false,
    settingsTabTitleGapInconsistent: false,
    settingsTabTitleGaps: {},
    // iOS viewport hardening (2026-09-03)
    iosInputZoomTargets: 0,
    viewportUnitTraps: 0,
    safeAreaInsetsDisabled: false,
    bottomPinnedWithoutSafeArea: 0,
    // A skipped or crashed view never ran the accessibility pass, so
    // axeAvailable is false rather than "zero violations found".
    axeAvailable: false,
    axeCritical: 0,
    axeSerious: 0,
    axeModerate: 0,
    axeMinor: 0,
    axeIncomplete: 0,
    layoutShiftSupported: false,
    layoutShiftValue: 0,
    layoutShiftCount: 0,
    consoleSeverityScore: 0,
    consoleSuppressed: 0,
    uiHealthScore: null,
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
        // Playwright cancels transient media/worker blob requests during
        // full-page capture. They are not application network failures.
        if (entry.url.startsWith('blob:') && /aborted/i.test(entry.error || '')) {
            continue;
        }
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
 * Applies the severity policy for metrics that are collected outside the
 * original layout checks. Keeping this in one place prevents findings from
 * disappearing between the browser payload, console summary, and results JSON.
 * @param {object} metrics
 * @param {string[]} failures
 * @param {string[]} warnings
 */
function applyMetricRules(metrics, failures, warnings) {
    if (metrics.trimInvalidRanges) failures.push('trim range is invalid');
    if (metrics.trimSnapViolations) warnings.push(`trim range is not snapped: ${metrics.trimSnapViolations}`);
    if (metrics.trimLoopDriftMs > 100) warnings.push(`trim loop drift: ${metrics.trimLoopDriftMs}ms`);
    if (metrics.trimZoomInstability) warnings.push(`trim waveform zoom instability: ${metrics.trimZoomInstability}`);
    if (metrics.trimHandleTooSmall) warnings.push(`trim handles below touch size: ${metrics.trimHandleTooSmall}`);
    if (metrics.trimKeyboardMissing?.length) {
        failures.push(`trim keyboard controls missing: ${metrics.trimKeyboardMissing.join(', ')}`);
    }
    if (metrics.trimKeyboardConflicts?.length) {
        failures.push(`trim keyboard conflicts: ${metrics.trimKeyboardConflicts.join(', ')}`);
    }
    if (metrics.trimInstanceLeaks) failures.push(`trim WaveSurfer instances leaked: ${metrics.trimInstanceLeaks}`);
    if (metrics.trimUiAudioMismatchMs > 250) {
        warnings.push(`trim UI/audio drift: ${metrics.trimUiAudioMismatchMs}ms`);
    }
    if (metrics.webkitOnlyRules > 10) warnings.push(`WebKit-only CSS rules: ${metrics.webkitOnlyRules}`);
    if (metrics.colorMixWithoutFallback) {
        warnings.push(`color-mix() declarations without fallback: ${metrics.colorMixWithoutFallback}`);
    }
    if (metrics.willChangeAbuse) warnings.push(`will-change abuse: ${metrics.willChangeAbuse}`);
    if (metrics.zIndexAbuse) warnings.push(`z-index values above 1200: ${metrics.zIndexAbuse}`);
    if (metrics.nestedOverflowHidden) warnings.push(`nested overflow:hidden containers: ${metrics.nestedOverflowHidden}`);
    if (metrics.mobileTitleCellFlexRegression) {
        failures.push('mobile title cell flex contract is broken');
    }
    if (metrics.mobileActionSurfaceContractBroken) {
        failures.push('mobile job controls or completion status are missing their raised surface contract');
    }
    if (metrics.platformBadgeGeometryIssues?.length) {
        failures.push(`platform badge geometry is not pixel-stable: ${metrics.platformBadgeGeometryIssues.length}`);
    }
    // iOS viewport hardening (2026-09-03)
    if (metrics.iosInputZoomTargets?.length) {
        const worst = metrics.iosInputZoomTargets
            .map((entry) => `${entry.id || entry.tag}@${entry.fontSize}px`)
            .slice(0, 5)
            .join(', ');
        failures.push(`controls below 16px font-size zoom the page on iOS focus: ${worst}`);
    }
    if (metrics.safeAreaInsetsDisabled) {
        failures.push('env(safe-area-inset-*) is used without viewport-fit=cover, so every safe-area value resolves to 0');
    }
    if (metrics.viewportUnitTraps?.length) {
        const worst = metrics.viewportUnitTraps
            .map((entry) => `${entry.selector} { ${entry.property}: ${entry.value} }`)
            .slice(0, 3)
            .join('; ');
        warnings.push(`vh height without dvh/svh fallback (iOS URL-bar): ${metrics.viewportUnitTraps.length} - ${worst}`);
    }
    if (metrics.bottomPinnedWithoutSafeArea?.length) {
        warnings.push(`elements pinned to the bottom edge without safe-area-inset-bottom: ${metrics.bottomPinnedWithoutSafeArea.length}`);
    }
}

/**
 * Formats one axe impact bucket as a single finding line, naming the rules
 * rather than only counting them so the report is actionable without opening
 * results.json.
 * @param {string} impact
 * @param {object[]} violations
 * @returns {string}
 */
function formatAxeBucket(impact, violations) {
    const rules = violations
        .map((violation) => `${violation.id}(${violation.nodeCount})`)
        .slice(0, 6)
        .join(', ');
    const more = violations.length > 6 ? `, +${violations.length - 6} more` : '';
    return `axe ${impact}: ${rules}${more}`;
}

/**
 * Routes axe violations into failures or warnings per UI_LINT_AXE_FAIL_ON.
 *
 * An audit that did not run is a warning naming the reason, never silence: a
 * missing package would otherwise read exactly like a clean accessibility
 * pass.
 * @param {object} axe
 * @param {string[]} failures
 * @param {string[]} warnings
 */
function applyAxeRules(axe, failures, warnings) {
    if (!axe.available) {
        warnings.push(`accessibility audit did not run: ${axe.error || 'unknown reason'}`);
        return;
    }

    const failing = AXE_FAIL_ON === 'none' ? [] : ['critical', ...(AXE_FAIL_ON === 'serious' ? ['serious'] : [])];

    for (const impact of ['critical', 'serious', 'moderate', 'minor']) {
        const violations = axe[impact] || [];
        if (!violations.length) continue;
        const line = formatAxeBucket(impact, violations);
        if (failing.includes(impact)) {
            failures.push(line);
        } else {
            warnings.push(line);
        }
    }

    if (axe.incomplete) {
        warnings.push(`axe could not decide ${axe.incomplete} checks (needs manual review)`);
    }
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
            await document.fonts.ready.catch(() => { });
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
        stableFrames: profileHasTouch(view.device) ? MOBILE_LAYOUT_STABLE_FRAMES : DESKTOP_LAYOUT_STABLE_FRAMES,
        maxFrames: profileHasTouch(view.device) ? MOBILE_LAYOUT_MAX_FRAMES : DESKTOP_LAYOUT_MAX_FRAMES,
        epsilon: LAYOUT_STABLE_EPSILON_PX,
    });
}

/**
 * Waits until media has either loaded or reported an error. Broken resources
 * remain visible so collectMetrics can report them instead of the linter
 * masking the failure with CSS.
 * @param {import('playwright').Page} page
 * @returns {Promise<void>}
 */
async function waitForMedia(page) {
    await page.waitForFunction(() => {
        const imagesReady = Array.from(document.images).every((image) => image.complete);
        const videosReady = Array.from(document.querySelectorAll('video')).every((video) => (
            video.readyState >= 2
            || video.error
            || (!video.currentSrc && !video.src)
        ));
        return imagesReady && videosReady;
    }, undefined, { timeout: 10000 }).catch(() => {
        // A slow remote media resource must not prevent the remaining audits.
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
    const evaluatePromise = page.evaluate(async ({
        requiredSelectors,
        mobileTouchTargetMin,
        desktopTouchTargetMin,
        isMobile,
        isPhone,
        isTouch,
        formFactor,
        auditPlatformBadges,
        platformBadgeWidth,
        platformBadgeHeight,
        platformBadgeIconSize,
        platformBadgeGeometryEpsilon,
    }) => {
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

        const isLayoutVisible = (el) => {
            if (!el || !el.isConnected) return false;
            if (el.closest('[hidden], .d-none, [aria-hidden="true"]')) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        const isLintInjectedSheet = (sheet) => (
            sheet.ownerNode?.dataset?.uiLintInjected === 'true'
        );

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

        const touchTargetMin = isTouch ? mobileTouchTargetMin : desktopTouchTargetMin;
        const smallTouchTargets = Array.from(document.querySelectorAll(interactiveSelector))
            .filter((el) => isVisible(el) && !el.disabled)
            .filter((el) => !isTouch || el.matches('button, .btn, [role="button"], input, select, textarea'))
            // Plain text links are not button-sized touch controls. Switch
            // inputs are intentionally compact; their form-check wrapper is
            // the accessible touch target.
            .filter((el) => !el.matches('a[href]:not(.btn):not([role="button"])'))
            .filter((el) => {
                if (!el.matches('input.form-check-input')) return true;
                const wrapper = el.closest('.form-check');
                if (!wrapper) return true;
                const rect = wrapper.getBoundingClientRect();
                return rect.width < touchTargetMin || rect.height < touchTargetMin;
            })
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

            const effectiveForeground = fg.a < 1 ? compositeOver(fg, bg) : fg;
            const fontSize = Number.parseFloat(style.fontSize);
            const fontWeight = Number.parseInt(style.fontWeight, 10);
            const isLargeText = fontSize >= 24 || (fontSize >= 18.66 && fontWeight >= 700);
            const requiredRatio = isLargeText ? 3 : 4.5;
            const ratio = contrastRatio(effectiveForeground, bg);

            if (ratio < requiredRatio) {
                contrastIssues.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    className: typeof el.className === 'string' ? el.className : '',
                    text: text.slice(0, 80),
                    ratio: ratio.toFixed(2),
                    requiredRatio,
                });
            }
        }

        const detectEscapedHTML = (root = document) => {
            const bad = [];
            root.querySelectorAll('*').forEach((el) => {
                if (!isVisible(el)) return;
                if (el.children.length === 0 && el.textContent.includes('<')) {
                    if (el.textContent.match(/<\w+/)) {
                        bad.push(el);
                    }
                }
            });
            return bad;
        };

        const escapedHtmlElements = detectEscapedHTML(document)
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
                text: (el.textContent || '').trim().slice(0, 120),
            }));

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
        const scriptSources = [];
        for (const script of Array.from(document.scripts)) {
            const inline = script.textContent?.trim() || '';
            if (inline) {
                scriptSources.push({ source: inline, url: window.location.href });
                continue;
            }

            const src = script.getAttribute('src') || '';
            if (!src) continue;

            try {
                const absolute = new URL(src, window.location.href);
                if (absolute.origin !== window.location.origin) continue;
                const response = await fetch(absolute.href, { credentials: 'same-origin' });
                if (!response.ok) continue;
                scriptSources.push({ source: await response.text(), url: absolute.href });
            } catch {
                // Ignore script fetch issues and continue with available sources.
            }
        }

        // Walk same-origin ES-module imports so checks do not depend on which
        // entry module happened to be listed in the HTML.
        const visitedModuleUrls = new Set();
        const moduleQueue = [...scriptSources];
        const importPattern = /\b(?:import|export)\s*(?:[^'";]*?\sfrom\s*)?['"]([^'"]+)['"]/g;
        while (moduleQueue.length && visitedModuleUrls.size < 256) {
            const { source, url } = moduleQueue.shift();
            if (visitedModuleUrls.has(url)) continue;
            visitedModuleUrls.add(url);
            scriptChunks.push(source);

            for (const match of source.matchAll(importPattern)) {
                const specifier = match[1];
                if (!specifier.startsWith('.') && !specifier.startsWith('/')) continue;
                try {
                    const importedUrl = new URL(specifier, url);
                    if (importedUrl.origin !== window.location.origin || visitedModuleUrls.has(importedUrl.href)) continue;
                    const response = await fetch(importedUrl.href, { credentials: 'same-origin' });
                    if (response.ok) {
                        moduleQueue.push({ source: await response.text(), url: importedUrl.href });
                    }
                } catch {
                    // Ignore optional or unavailable imports and continue linting.
                }
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
        // The dashboard deliberately mixes a two-column stats grid with full
        // width cards. Only compare cards that belong to the same settings
        // grid; comparing all dashboard cards creates a false positive.
        const leftEdges = Array.from(document.querySelectorAll('.settings-grid-item > .ui-card'))
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

        const localOverflowIssues = isMobile
            ? Array.from(document.querySelectorAll('body *'))
                .filter((el) => isLayoutVisible(el))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    return {
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                        className: typeof el.className === 'string' ? el.className : '',
                        left: Math.round(rect.left),
                        right: Math.round(rect.right),
                    };
                })
                .filter((entry) => entry.right > window.innerWidth + 1 || entry.left < -1)
                .slice(0, 20)
            : [];

        const focusStyleSnapshot = (el) => {
            const style = window.getComputedStyle(el);
            return {
                outline: style.outline,
                boxShadow: style.boxShadow,
                borderColor: style.borderColor,
                backgroundColor: style.backgroundColor,
            };
        };

        const focusIndicatorMissing = [];
        const focusable = Array.from(document.querySelectorAll('button, a, input, select, textarea, [tabindex]'))
            .filter((el) => isVisible(el) && !el.disabled)
            .slice(0, 25);

        for (const el of focusable) {
            const before = focusStyleSnapshot(el);
            try {
                el.focus({ preventScroll: true });
            } catch {
                // Ignore focus failures and keep scanning the remaining controls.
            }

            const after = focusStyleSnapshot(el);
            if (document.activeElement === el) {
                const changed =
                    before.outline !== after.outline ||
                    before.boxShadow !== after.boxShadow ||
                    before.borderColor !== after.borderColor ||
                    before.backgroundColor !== after.backgroundColor;

                if (!changed) {
                    focusIndicatorMissing.push(el.tagName.toLowerCase());
                }
            }

            if (typeof el.blur === 'function') {
                el.blur();
            }
        }

        const iconPointerEventsIssues = Array.from(document.querySelectorAll('button .material-symbols-outlined'))
            .filter((icon) => window.getComputedStyle(icon).pointerEvents !== 'none')
            .map((icon) => ({
                tag: icon.tagName.toLowerCase(),
                id: icon.id || null,
                className: typeof icon.className === 'string' ? icon.className : '',
            }));

        const ghostScrollContainers = Array.from(document.querySelectorAll('*'))
            .filter((el) => {
                if (!isLayoutVisible(el)) return false;
                const style = window.getComputedStyle(el);
                if (style.overflowY !== 'auto' && style.overflowY !== 'scroll') return false;
                const delta = el.scrollHeight - el.clientHeight;
                return delta > 0 && delta < 8 && el.clientHeight > 40;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const scrollContainers = Array.from(document.querySelectorAll('*'))
            .filter((el) => {
                if (!isLayoutVisible(el)) return false;
                const style = window.getComputedStyle(el);
                const isScrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
                return isScrollable && el.scrollHeight > el.clientHeight + 2;
            });

        // iOS Scroll Blocking / Layout Bug Detection
        // 1. Parent with overflow:hidden blocking scrollable child
        const overflowHiddenScrollBlockers = [];
        const overflowHiddenSeen = new Set();
        for (const el of scrollContainers) {
            let parent = el.parentElement;
            while (parent && parent !== document.body) {
                const style = window.getComputedStyle(parent);
                if (
                    style.overflow === 'hidden' ||
                    style.overflowY === 'hidden'
                ) {
                    const key = `${parent.tagName}|${parent.className || ''}`;
                    if (!overflowHiddenSeen.has(key)) {
                        overflowHiddenSeen.add(key);
                        overflowHiddenScrollBlockers.push({
                            child: el.tagName.toLowerCase(),
                            parent: parent.tagName.toLowerCase(),
                            parentClass: parent.className || '',
                        });
                    }
                    break;
                }
                parent = parent.parentElement;
            }
        }

        const flexMinHeightOverflowHidden = Array.from(document.querySelectorAll('*'))
            .filter((el) => {
                if (!isLayoutVisible(el)) return false;
                const style = window.getComputedStyle(el);
                const isFlex = style.display.includes('flex');
                const minHeightZero = style.minHeight === '0px' || style.minHeight === '0';
                const overflowHidden = style.overflow === 'hidden' || style.overflowY === 'hidden';

                if (!(isFlex && minHeightZero && overflowHidden)) return false;

                const clipsContent = el.scrollHeight > el.clientHeight + 2;
                if (!clipsContent) return false;

                const hasDescendantScroller = Array.from(el.querySelectorAll('*')).some((child) => {
                    const childStyle = window.getComputedStyle(child);
                    const childCanScroll = childStyle.overflowY === 'auto' || childStyle.overflowY === 'scroll';
                    return childCanScroll && child.scrollHeight > child.clientHeight + 2;
                });

                return !hasDescendantScroller;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }))
            .slice(0, 20);

        // 2. Dangerous usage of :has() with layout-critical properties
        const hasSelectorLayoutUsage = [];
        const hasSelectorSeen = new Set();
        for (const sheet of document.styleSheets) {
            let rules;
            try {
                rules = sheet.cssRules;
            } catch {
                continue;
            }
            for (const rule of rules) {
                if (!rule.selectorText) continue;
                if (rule.selectorText.includes(':has(')) {
                    const css = rule.cssText || '';
                    if (
                        css.includes('overflow') ||
                        css.includes('height') ||
                        css.includes('position')
                    ) {
                        if (!hasSelectorSeen.has(rule.selectorText)) {
                            hasSelectorSeen.add(rule.selectorText);
                            hasSelectorLayoutUsage.push(rule.selectorText);
                        }
                    }
                }
            }
        }

        const nestedScrollContainers = scrollContainers.filter((el) => {
            let parent = el.parentElement;
            while (parent) {
                if (scrollContainers.includes(parent)) return true;
                parent = parent.parentElement;
            }
            return false;
        }).map((el) => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            className: typeof el.className === 'string' ? el.className : '',
        }));

        const pageScrollable = document.documentElement.scrollHeight > window.innerHeight + 4;
        const doubleScrollRisk = pageScrollable && scrollContainers.length > 0 ? 1 : 0;
        const jobsTableScroller = document.querySelector('#jobsCard .jobs-list-shell');
        const viewportScrollLeak = (() => {
            if (!jobsTableScroller || !isLayoutVisible(jobsTableScroller)) return 0;
            const style = window.getComputedStyle(jobsTableScroller);
            const localScroll = (style.overflowY === 'auto' || style.overflowY === 'scroll')
                && jobsTableScroller.scrollHeight > jobsTableScroller.clientHeight + 2;
            return localScroll && pageScrollable ? 1 : 0;
        })();

        // 3. Viewport locking via vh/dvh + overflow hidden (iOS trap)
        const viewportLockingIssues = (() => {
            const root = document.documentElement;
            const body = document.body;
            const issues = [];

            for (const el of [root, body]) {
                const style = window.getComputedStyle(el);

                const hasVh =
                    /(vh|dvh|svh)/.test(style.height) ||
                    /(vh|dvh|svh)/.test(style.minHeight);

                const overflowHidden =
                    style.overflow === 'hidden' ||
                    style.overflowY === 'hidden';

                if (hasVh && overflowHidden) {
                    issues.push({
                        element: el.tagName.toLowerCase(),
                        height: style.height,
                        overflow: style.overflow,
                    });
                }
            }
            return issues;
        })();

        const flexScrollTraps = Array.from(document.querySelectorAll('*'))
            .filter((el) => {
                if (!isLayoutVisible(el)) return false;
                const parent = el.parentElement;
                if (!parent) return false;

                const parentStyle = window.getComputedStyle(parent);
                const style = window.getComputedStyle(el);
                const isFlex = parentStyle.display.includes('flex');
                const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
                const hasOverflow = el.scrollHeight > el.clientHeight + 2;
                const minHeightZero = style.minHeight === '0px' || style.minHeight === '0';

                return isFlex && scrollable && hasOverflow && !minHeightZero;
            })
            .map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        const badges = Array.from(document.querySelectorAll('.badge')).filter((el) => isVisible(el));
        let badgeInconsistencies = 0;
        if (badges.length > 1) {
            const base = window.getComputedStyle(badges[0]);
            for (const el of badges.slice(1)) {
                const style = window.getComputedStyle(el);
                if (style.borderRadius !== base.borderRadius || style.fontWeight !== base.fontWeight) {
                    badgeInconsistencies += 1;
                }
            }
        }

        // Platform badge geometry contract: the badge and its icon use fixed
        // integer CSS-pixel boxes, never shrink in title layouts, have no
        // padding, and share an exact geometric centre. Synthetic fixtures
        // keep the rule active even when the current database has no jobs;
        // visible badges additionally verify the real desktop/mobile layout.
        const platformBadgeGeometryIssues = [];
        if (auditPlatformBadges) {
            const inspectPlatformBadge = (pill, source) => {
                const icon = pill.querySelector('.platform-pill__icon');
                const pillStyle = window.getComputedStyle(pill);
                const pillRect = pill.getBoundingClientRect();
                const addIssue = (type, values = {}) => {
                    platformBadgeGeometryIssues.push({ source, type, ...values });
                };
                const differs = (actual, expected) => (
                    Math.abs(actual - expected) > platformBadgeGeometryEpsilon
                );

                if (differs(pillRect.width, platformBadgeWidth)) {
                    addIssue('badge-width', { actual: pillRect.width, expected: platformBadgeWidth });
                }
                if (differs(pillRect.height, platformBadgeHeight)) {
                    addIssue('badge-height', { actual: pillRect.height, expected: platformBadgeHeight });
                }
                if (pillStyle.boxSizing !== 'border-box') {
                    addIssue('badge-box-sizing', { actual: pillStyle.boxSizing });
                }
                if (pillStyle.alignItems !== 'center' || pillStyle.justifyContent !== 'center') {
                    addIssue('badge-flex-alignment', {
                        alignItems: pillStyle.alignItems,
                        justifyContent: pillStyle.justifyContent,
                    });
                }
                if (pillStyle.flexShrink !== '0') {
                    addIssue('badge-can-shrink', { actual: pillStyle.flexShrink });
                }

                const padding = [
                    pillStyle.paddingTop,
                    pillStyle.paddingRight,
                    pillStyle.paddingBottom,
                    pillStyle.paddingLeft,
                ].map((value) => Number.parseFloat(value) || 0);
                if (padding.some((value) => value > platformBadgeGeometryEpsilon)) {
                    addIssue('badge-padding', { actual: padding });
                }

                if (!icon) {
                    addIssue('missing-icon');
                    return;
                }

                const iconStyle = window.getComputedStyle(icon);
                const iconRect = icon.getBoundingClientRect();
                if (differs(iconRect.width, platformBadgeIconSize)
                    || differs(iconRect.height, platformBadgeIconSize)) {
                    addIssue('icon-size', {
                        width: iconRect.width,
                        height: iconRect.height,
                        expected: platformBadgeIconSize,
                    });
                }
                if (iconStyle.flexShrink !== '0') {
                    addIssue('icon-can-shrink', { actual: iconStyle.flexShrink });
                }

                const badgeCenterX = pillRect.left + (pillRect.width / 2);
                const badgeCenterY = pillRect.top + (pillRect.height / 2);
                const iconCenterX = iconRect.left + (iconRect.width / 2);
                const iconCenterY = iconRect.top + (iconRect.height / 2);
                const deltaX = Math.abs(badgeCenterX - iconCenterX);
                const deltaY = Math.abs(badgeCenterY - iconCenterY);
                if (deltaX > platformBadgeGeometryEpsilon || deltaY > platformBadgeGeometryEpsilon) {
                    addIssue('icon-not-centered', { deltaX, deltaY });
                }
            };

            Array.from(document.querySelectorAll('.platform-pill'))
                .filter((pill) => isLayoutVisible(pill))
                .forEach((pill, index) => inspectPlatformBadge(pill, `rendered-${index}`));

            const fixtureHost = document.createElement('div');
            fixtureHost.dataset.uiLintInjected = 'true';
            fixtureHost.style.cssText = [
                'position:fixed',
                'left:-200px',
                'top:-200px',
                'display:flex',
                'width:20px',
                'visibility:hidden',
                'pointer-events:none',
            ].join(';');
            document.body.appendChild(fixtureHost);

            for (const platform of ['youtube', 'tiktok', 'instagram', 'facebook']) {
                const pill = document.createElement('span');
                pill.className = `platform-pill platform-pill--${platform}`;
                const icon = document.createElement('span');
                icon.className = `platform-pill__icon platform-pill__icon--${platform}`;
                pill.appendChild(icon);

                if (isMobile) {
                    const mobileContext = document.createElement('article');
                    mobileContext.className = 'job-item';
                    mobileContext.style.cssText = 'display:flex;width:20px';
                    mobileContext.appendChild(pill);
                    fixtureHost.replaceChildren(mobileContext);
                } else {
                    fixtureHost.replaceChildren(pill);
                }

                inspectPlatformBadge(pill, `fixture-${isMobile ? 'mobile-' : ''}${platform}`);
            }
            fixtureHost.remove();
        }

        const gridViolations = Array.from(document.querySelectorAll('.row'))
            .filter((row) => isLayoutVisible(row))
            .filter((row) => !Array.from(row.children).some((child) => {
                const className = typeof child.className === 'string' ? child.className : '';
                return className.includes('col');
            }))
            .map((row) => ({
                tag: row.tagName.toLowerCase(),
                id: row.id || null,
                className: typeof row.className === 'string' ? row.className : '',
            }));

        const overlapIssues = [];
        for (const row of document.querySelectorAll('table tbody tr')) {
            if (!isLayoutVisible(row)) continue;
            const cells = Array.from(row.children).filter((cell) => isLayoutVisible(cell));
            for (let index = 0; index < cells.length - 1; index += 1) {
                const current = cells[index].getBoundingClientRect();
                const next = cells[index + 1].getBoundingClientRect();
                // Only flag real visual overlaps: cells must be on the same horizontal
                // row (vertical overlap) AND have crossing right/left edges.
                // Single-column stacked layouts share x-coordinates but not y-range.
                const horizontalCross = current.right > next.left + 1;
                const verticalOverlap = current.bottom > next.top + 1 && current.top < next.bottom - 1;
                if (horizontalCross && verticalOverlap) {
                    overlapIssues.push({
                        rowTag: row.tagName.toLowerCase(),
                        rowId: row.id || null,
                    });
                    break;
                }
            }
        }

        const brokenTitleTruncation = isMobile
            ? Array.from(document.querySelectorAll('.job-title-text, .job-title-cell'))
                .filter((el) => isLayoutVisible(el))
                .filter((el) => {
                    const style = window.getComputedStyle(el);
                    if (el.classList.contains('job-title-text')) {
                        const lineClamp = style.webkitLineClamp || style.getPropertyValue('-webkit-line-clamp');
                        const isMultiLineClamp = Number.parseInt(lineClamp || '0', 10) >= 1;
                        return style.overflow !== 'hidden'
                            || (!isMultiLineClamp && (style.textOverflow !== 'ellipsis' || style.whiteSpace !== 'nowrap'));
                    }

                    if (el.classList.contains('job-title-cell')) {
                        return style.minWidth !== '0px';
                    }

                    return false;
                })
                .map((el) => ({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    className: typeof el.className === 'string' ? el.className : '',
                }))
            : [];

        // Mobile jobs feed contract:
        // - the feed is opt-in and hidden together with the jobs card by default
        // - when enabled, the feed list is visible while the desktop view is hidden
        // - rows render as article.job-item nodes, not table rows
        // - each job exposes exactly one visible primary action
        // - no per-row dropdown/action cluster is visible in the feed
        // Tablet contract. app/static/style.css swaps the desktop table for
        // the feed at `@media (max-width: 1024px)`, so an iPad sits on either
        // side of that line depending on model and orientation - and neither
        // the 390px phone context nor the 1440px desktop one ever renders the
        // band in between.
        //
        // Below the breakpoint the feed must render (as on the phone); above
        // it the desktop table must render and fit. Either way the shell has
        // to use the width the device actually has.
        const tabletLayoutIssues = formFactor === 'tablet'
            && document.body.classList.contains('app-root--dashboard')
            ? (() => {
                const issues = [];
                const compact = window.innerWidth <= 1024;
                const mobileList = document.querySelector('#jobsMobileList');
                const desktopView = document.querySelector('.jobs-desktop-view');
                const displayed = (el) => Boolean(el) && window.getComputedStyle(el).display !== 'none';

                if (compact) {
                    if (!displayed(mobileList)) {
                        issues.push({ type: 'feed-not-rendered-below-breakpoint', width: window.innerWidth });
                    }
                    if (displayed(desktopView)) {
                        issues.push({ type: 'desktop-table-still-rendered-below-breakpoint', width: window.innerWidth });
                    }
                } else {
                    if (!displayed(desktopView)) {
                        issues.push({ type: 'desktop-table-not-rendered-above-breakpoint', width: window.innerWidth });
                    }
                    if (displayed(mobileList)) {
                        issues.push({ type: 'feed-still-rendered-above-breakpoint', width: window.innerWidth });
                    }

                    const table = document.querySelector('#jobsTable');
                    if (table instanceof HTMLElement) {
                        const scroller = table.closest('.jobs-list-shell') || table.parentElement;
                        if (scroller instanceof HTMLElement && table.scrollWidth - scroller.clientWidth > 1) {
                            issues.push({
                                type: 'jobs-table-overflows',
                                overflowPx: Math.round(table.scrollWidth - scroller.clientWidth),
                            });
                        }
                    }
                }

                // A max-width tuned for a 1440px desktop leaves an iPad with
                // dead margins instead of the screen it actually has.
                const shell = document.querySelector('.app-main') || document.querySelector('main');
                if (shell instanceof HTMLElement) {
                    const used = shell.getBoundingClientRect().width;
                    if (used > 0 && used < window.innerWidth * 0.85) {
                        issues.push({
                            type: 'shell-underuses-tablet-width',
                            usedPx: Math.round(used),
                            viewportPx: window.innerWidth,
                        });
                    }
                }

                return issues;
            })()
            : [];

        const mobileJobsFeedIssues = isMobile && document.body.classList.contains('app-root--dashboard')
            ? (() => {
                const issues = [];
                const mobileList = document.querySelector('#jobsMobileList');
                const desktopView = document.querySelector('.jobs-desktop-view');
                const jobsCard = document.querySelector('#jobsCard');
                const historyToggle = document.querySelector('#showJobHistoryToggle');
                if (!mobileList) {
                    return [{ type: 'missing-mobile-list' }];
                }

                const jobsHeader = jobsCard?.querySelector('.jobs-card-header');
                const jobsShell = jobsCard?.querySelector('.jobs-list-shell');
                if (jobsHeader instanceof HTMLElement && jobsShell instanceof HTMLElement) {
                    const headerStyle = window.getComputedStyle(jobsHeader);
                    const shellStyle = window.getComputedStyle(jobsShell);
                    const listStyle = window.getComputedStyle(mobileList);
                    const headerInset = Number.parseFloat(headerStyle.paddingLeft) || 0;
                    const feedInset = (Number.parseFloat(shellStyle.paddingLeft) || 0)
                        + (Number.parseFloat(listStyle.paddingLeft) || 0);
                    if (Math.abs(headerInset - feedInset) > 0.1) {
                        issues.push({
                            type: 'feed-edge-inset-mismatch',
                            headerInset,
                            feedInset,
                        });
                    }
                }

                if (!(historyToggle instanceof HTMLInputElement) || !isLayoutVisible(historyToggle)) {
                    issues.push({ type: 'missing-job-history-toggle' });
                }

                if (!(historyToggle instanceof HTMLInputElement) || !historyToggle.checked) {
                    if (jobsCard && isLayoutVisible(jobsCard)) {
                        issues.push({ type: 'jobs-card-visible-by-default' });
                    }
                    if (isLayoutVisible(mobileList)) {
                        issues.push({ type: 'mobile-list-visible-when-disabled' });
                    }
                    return issues;
                }

                if (!isLayoutVisible(mobileList)) {
                    issues.push({ type: 'mobile-list-hidden' });
                }

                if (desktopView && isLayoutVisible(desktopView)) {
                    issues.push({ type: 'desktop-view-visible' });
                }

                const jobItems = Array.from(mobileList.querySelectorAll('article.job-item[data-job-id]'));
                for (const item of jobItems) {
                    if (!isLayoutVisible(item)) continue;

                    const rowId = item.dataset.jobId;
                    const itemStyle = window.getComputedStyle(item);
                    if (!itemStyle.display.includes('flex')) {
                        issues.push({ type: 'job-not-flex', rowId });
                    }

                    if (item.querySelector('table, tr, td, th')) {
                        issues.push({ type: 'table-markup-in-feed', rowId });
                    }

                    const body = item.querySelector('.job-item__body');
                    if (!(body instanceof HTMLElement) || !isLayoutVisible(body)) {
                        issues.push({ type: 'missing-body', rowId });
                    }

                    const meta = item.querySelector('.job-item__meta');
                    if (!(meta instanceof HTMLElement) || !isLayoutVisible(meta)) {
                        issues.push({ type: 'missing-meta', rowId });
                    }

                    const primaryActionWrap = item.querySelector('.job-item__primary-action');
                    if (!(primaryActionWrap instanceof HTMLElement) || !isLayoutVisible(primaryActionWrap)) {
                        issues.push({ type: 'missing-primary-action-wrap', rowId });
                        continue;
                    }

                    const primaryActions = primaryActionWrap.querySelectorAll('.jobs-mobile-action');
                    if (primaryActions.length !== 1) {
                        issues.push({ type: 'wrong-primary-action-count', count: primaryActions.length, rowId });
                    }

                    const extraActionUis = Array.from(item.querySelectorAll('.job-item__actions, .btn-group, .dropdown-menu, .dropdown-toggle'))
                        .filter((el) => isLayoutVisible(el));
                    if (extraActionUis.length > 0) {
                        issues.push({ type: 'secondary-actions-visible', count: extraActionUis.length, rowId });
                    }

                    const titleText = item.querySelector('.job-title-text');
                    if (titleText instanceof HTMLElement) {
                        const titleStyle = window.getComputedStyle(titleText);
                        const lineClamp = titleStyle.webkitLineClamp || titleStyle.getPropertyValue('-webkit-line-clamp');
                        if (titleStyle.overflow !== 'hidden' || Number.parseInt(lineClamp || '0', 10) < 2) {
                            issues.push({ type: 'title-not-clamped', rowId });
                        }
                    } else {
                        issues.push({ type: 'missing-title', rowId });
                    }
                }

                return issues.slice(0, 20);
            })()
            : [];

        // Stat-card vertical centering: .ui-card--stat > div must have flex: 0 0 auto
        // (not flex: 1) so justify-content: center actually works.
        const statCardCenteringIssues = (() => {
            const issues = [];
            const cards = document.querySelectorAll('.ui-card--stat.stat-card');
            for (const card of cards) {
                if (!isLayoutVisible(card)) continue;
                const cardStyle = window.getComputedStyle(card);
                // Card itself must be flex + column
                if (cardStyle.display !== 'flex' || cardStyle.flexDirection !== 'column') {
                    issues.push({ type: 'stat-card-not-flex-column', id: card.id || null });
                    continue;
                }
                // Direct div children must not expand (flex grow must be 0)
                for (const child of card.children) {
                    if (child.tagName !== 'DIV') continue;
                    const cs = window.getComputedStyle(child);
                    if (cs.flexGrow !== '0') {
                        issues.push({ type: 'stat-card-child-expands', id: card.id || null, childClass: child.className });
                        break;
                    }
                }
            }
            return issues;
        })();

        const mixedLayoutIssues = isMobile
            ? Array.from(document.querySelectorAll('.stats-row'))
                .filter((row) => {
                    const style = window.getComputedStyle(row);
                    // Skip Bootstrap .row containers: having both a CSS grid override and
                    // Bootstrap col-* children is intentional when the CSS explicitly
                    // neutralizes the col-* classes with !important overrides.
                    if (row.classList.contains('row')) return false;
                    return style.display === 'grid' && Boolean(row.querySelector('.col-6, .col-md-3'));
                })
                .map((row) => ({
                    tag: row.tagName.toLowerCase(),
                    id: row.id || null,
                    className: typeof row.className === 'string' ? row.className : '',
                }))
            : [];

        const containerWidthIssue = isPhone
            ? (() => {
                const container = document.querySelector('.app-shell-container');
                if (!container || !isLayoutVisible(container)) return false;
                const rect = container.getBoundingClientRect();
                return rect.width < window.innerWidth - 12 || rect.left > 4 || (window.innerWidth - rect.right) > 4;
            })()
            : false;

        // The dashboard's four stat tiles (.stats-row) must render as one
        // row of four on mobile (not a 2x2 wrap), and within each tile the
        // icon must center over the *whole* "number + unit" block
        // (.stat-num-line) -- not just the bare number -- so a tile with a
        // unit (e.g. "20.1 MiB") does not visually shift its icon off to
        // one side. The row also keeps the same visual gap to the navbar and
        // the submit card so it does not float low in the mobile viewport.
        const statTileMobileLayoutIssues = isPhone
            ? (() => {
                const issues = [];
                // Per row, not page-wide: Settings -> System stacks the job
                // tiles above the host tiles, and two rows of four are two
                // correct single rows -- not one wrapped row of eight.
                const rows = Array.from(document.querySelectorAll('.stats-row'))
                    .filter((row) => isLayoutVisible(row));
                const cards = rows
                    .flatMap((row) => Array.from(row.querySelectorAll('.stat-card')))
                    .filter((card) => isLayoutVisible(card));

                for (const row of rows) {
                    const rowCards = Array.from(row.querySelectorAll('.stat-card'))
                        .filter((card) => isLayoutVisible(card));
                    if (rowCards.length < 2) continue;
                    const tops = new Set(rowCards.map((card) => Math.round(card.getBoundingClientRect().top)));
                    if (tops.size > 1) {
                        issues.push({ type: 'stat-tiles-not-single-row', rows: tops.size });
                    }
                }

                const navbar = document.querySelector('.top-navbar');
                const submitCard = document.querySelector('#submitCard');
                if (cards.length > 0
                    && navbar instanceof HTMLElement
                    && submitCard instanceof HTMLElement
                    && isLayoutVisible(navbar)
                    && isLayoutVisible(submitCard)) {
                    const navbarRect = navbar.getBoundingClientRect();
                    const submitRect = submitCard.getBoundingClientRect();
                    const cardRects = cards.map((card) => card.getBoundingClientRect());
                    const topGap = Math.min(...cardRects.map((rect) => rect.top)) - navbarRect.bottom;
                    const bottomGap = submitRect.top - Math.max(...cardRects.map((rect) => rect.bottom));
                    if (Math.abs(topGap - bottomGap) > 1) {
                        issues.push({
                            type: 'stat-row-vertical-gap-mismatch',
                            topGap: Math.round(topGap * 10) / 10,
                            bottomGap: Math.round(bottomGap * 10) / 10,
                        });
                    }
                }

                for (const card of cards) {
                    const icon = card.querySelector('.stat-icon');
                    const numLine = card.querySelector('.stat-num-line');
                    if (!icon || !numLine) continue;
                    const iconRect = icon.getBoundingClientRect();
                    const numLineRect = numLine.getBoundingClientRect();
                    const iconCenter = iconRect.left + iconRect.width / 2;
                    const numLineCenter = numLineRect.left + numLineRect.width / 2;
                    if (Math.abs(iconCenter - numLineCenter) > 2) {
                        issues.push({
                            type: 'stat-icon-not-centered-over-metric',
                            key: card.dataset.statKey || null,
                            deltaPx: Math.round(iconCenter - numLineCenter),
                        });
                    }
                }

                return issues;
            })()
            : [];

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
            const offsetCandidates = [
                document.body,
                document.querySelector('.app-root'),
                document.querySelector('.app-shell'),
                document.querySelector('.app-main'),
            ].filter(Boolean);

            const maxTopOffset = offsetCandidates.reduce((maxOffset, el) => {
                const paddingTop = parseInt(window.getComputedStyle(el).paddingTop || '0', 10);
                return Number.isFinite(paddingTop) ? Math.max(maxOffset, paddingTop) : maxOffset;
            }, 0);

            insufficientFixedTopOffset = maxTopOffset < 40;
        }

        const footer = document.querySelector('.wb-footer');
        let footerNotFlex = false;
        let footerSpaceReservationMissing = false;
        if (footer) {
            const footerStyle = window.getComputedStyle(footer);
            footerNotFlex = footerStyle.display !== 'flex';

            const shell = document.querySelector('.app-shell');
            const main = document.querySelector('.app-main');
            const shellStyle = shell ? window.getComputedStyle(shell) : null;
            const mainStyle = main ? window.getComputedStyle(main) : null;
            const footerRect = footer.getBoundingClientRect();
            const mainRect = main?.getBoundingClientRect();
            const footerIsInFlow = footerStyle.position === 'static'
                || footerStyle.position === 'relative';
            const mainAndFooterDoNotOverlap = !mainRect
                || footerRect.top >= mainRect.bottom - 4;
            const footerHeight = footerRect.height;
            const mainPaddingBottom = Number.parseFloat(mainStyle?.paddingBottom || '0');
            const explicitPaddingReservation = mainPaddingBottom >= footerHeight - 4;
            const flexShell = Boolean(shellStyle)
                && shellStyle.display.includes('flex')
                && shellStyle.flexDirection === 'column';
            const stableFooterItem = footerStyle.flexGrow === '0'
                && footerStyle.flexShrink === '0';

            // A normal-flow footer reserves its own layout box. A sticky or
            // fixed footer must instead be backed by equivalent main padding,
            // otherwise expanding content can be hidden underneath it.
            const hasReservation = footerIsInFlow
                ? mainAndFooterDoNotOverlap
                : explicitPaddingReservation;
            footerSpaceReservationMissing = !(flexShell && stableFooterItem && hasReservation);
        }

        const footerViewportPlacementBroken = (() => {
            if (!footer || !isLayoutVisible(footer)) return false;
            const rect = footer.getBoundingClientRect();
            const shortPage = document.documentElement.scrollHeight <= window.innerHeight + 4;
            return shortPage && Math.abs(rect.bottom - window.innerHeight) > 4;
        })();

        // Check that all visible buttons inside the fixed navbar are vertically
        // centred. A button is misaligned when its visual midpoint deviates more
        // than 3 px from the navbar's own midpoint.
        const navbarButtonAlignment = (() => {
            const nav = document.querySelector('.top-navbar, nav.fixed-top, nav[class*="top-navbar"]');
            if (!nav) return 0;
            const navRect = nav.getBoundingClientRect();
            const navMid = navRect.top + navRect.height / 2;
            const buttons = Array.from(nav.querySelectorAll('.btn, button, a[role="button"]'))
                .filter((el) => isVisible(el));
            const misaligned = buttons.filter((el) => {
                const rect = el.getBoundingClientRect();
                const elMid = rect.top + rect.height / 2;
                return Math.abs(elMid - navMid) > 3;
            });
            return misaligned.length;
        })();

        // CSS review checks

        // 1. Undefined CSS Custom Properties
        const undefinedCustomProperties = (() => {
            const defined = new Set();
            const used = new Map();
            const varPattern = /var\(\s*(--[\w-]+)/g;

            // Collect defined vars from ALL rules across all accessible sheets
            // (Bootstrap defines --bs-* on component selectors, not only :root,
            //  so a :root-only scan produces hundreds of false positives).
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (rule.style) {
                            for (let i = 0; i < rule.style.length; i++) {
                                const p = rule.style[i];
                                if (p.startsWith('--')) defined.add(p);
                            }
                        }
                    }
                } catch { /* cross-origin sheet – skip */ }
            }

            // Collect var() usages only from app-own stylesheets so we don't
            // report violations in vendor code that we can't fix.
            for (const sheet of document.styleSheets) {
                try {
                    const href = sheet.href ? new URL(sheet.href).pathname : '';
                    if (href.includes('/vendor/') || href.includes('bootstrap') || isLintInjectedSheet(sheet)) continue;
                    for (const rule of sheet.cssRules) {
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

            // Report vars used in our own CSS but not defined anywhere.
            const missing = [];
            for (const [prop, selectors] of used.entries()) {
                if (!defined.has(prop)) {
                    missing.push({ property: prop, usedIn: selectors.slice(0, 3) });
                }
            }
            return missing;
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
            let hasGlobalReducedMotionOverride = false;
            const unguarded = [];

            const walkRules = (rules, visit) => {
                for (const rule of rules) {
                    visit(rule);
                    if (rule.cssRules?.length) walkRules(rule.cssRules, visit);
                }
            };

            const isInsideReducedMotionMedia = (rule) => {
                let parent = rule.parentRule;
                while (parent) {
                    if (parent instanceof CSSMediaRule
                        && parent.conditionText?.includes('prefers-reduced-motion')) {
                        return true;
                    }
                    parent = parent.parentRule;
                }
                return false;
            };

            for (const sheet of document.styleSheets) {
                try {
                    // Skip vendor/third-party sheets – we can't fix Bootstrap animations.
                    const href = sheet.href ? new URL(sheet.href).pathname : '';
                    if (href.includes('/vendor/') || href.includes('bootstrap') || isLintInjectedSheet(sheet)) continue;
                    walkRules(sheet.cssRules, (rule) => {
                        if (rule instanceof CSSMediaRule) {
                            if (rule.conditionText?.includes('prefers-reduced-motion')) {
                                hasReducedMotionMedia = true;
                                if (rule.conditionText.includes('prefers-reduced-motion: reduce')) {
                                    hasGlobalReducedMotionOverride = Array.from(rule.cssRules || []).some((child) => (
                                        child.selectorText?.includes('*')
                                        && animatedProps.some((prop) => child.style?.getPropertyValue(prop))
                                    ));
                                }
                            }
                        }
                        if (!rule.style) return;
                        for (const prop of animatedProps) {
                            const value = rule.style.getPropertyValue(prop);
                            if (value && value !== 'none' && !value.includes('0s')) {
                                if (!isInsideReducedMotionMedia(rule)) {
                                    unguarded.push({
                                        selector: rule.selectorText || '(unknown)',
                                        property: prop,
                                    });
                                }
                            }
                        }
                    });
                } catch { /* cross-origin */ }
            }

            return {
                hasReducedMotionMedia,
                hasGlobalReducedMotionOverride,
                unguarded: hasGlobalReducedMotionOverride ? [] : unguarded.slice(0, 15),
            };
        })();

        // 6. Dropdowns inside overflow:hidden containers
        const clippedDropdowns = Array.from(
            document.querySelectorAll('.dropdown-menu, [role="menu"], [role="listbox"]')
        ).filter(el => {
            if (window.getComputedStyle(el).position === 'fixed') return false;
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

        // 6b. Jobs action buttons should not sit flush against the right edge.
        const jobsActionRightEdgeSpacing = Array.from(
            document.querySelectorAll('#jobsTable tbody td.col-actions, #jobsTable tbody td[data-label="Action"]')
        ).flatMap((cell) => {
            const cellStyle = window.getComputedStyle(cell);
            if (cellStyle.display === 'none' || cellStyle.visibility === 'hidden') {
                return [];
            }

            const lastButton = cell.querySelector('.btn-group > .btn:last-of-type, .action-buttons .btn:last-of-type');
            if (!lastButton) {
                return [];
            }

            const cellRect = cell.getBoundingClientRect();
            const buttonRect = lastButton.getBoundingClientRect();
            if (cellRect.width <= 0 || buttonRect.width <= 0) {
                return [];
            }

            const rightGap = Math.round((cellRect.right - buttonRect.right) * 10) / 10;
            const minGapPx = 8;
            if (rightGap >= minGapPx) {
                return [];
            }

            const row = cell.closest('tr');
            return [{
                rowId: row?.dataset?.jobId || null,
                rightGap,
                minGapPx,
                cellWidth: Math.round(cellRect.width * 10) / 10,
                buttonWidth: Math.round(buttonRect.width * 10) / 10,
            }];
        }).slice(0, 20);

        // 7. !important abuse check (app stylesheets only – Bootstrap has ~800 legitimate ones)
        const importantAbuse = (() => {
            let count = 0;
            const violations = [];
            for (const sheet of document.styleSheets) {
                try {
                    const href = sheet.href ? new URL(sheet.href).pathname : '';
                    if (href.includes('/vendor/') || href.includes('bootstrap') || isLintInjectedSheet(sheet)) continue;
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
            return { count: violations.length, total: count, violations: violations.slice(0, 20) };
        })();

        // CSS advanced lint checks (2026-04-29)
        const cssAdvancedLint = (() => {
            let webkitOnlyRules = 0;
            let colorMixTotal = 0;
            let colorMixWithFallback = 0;
            let hasColorMixFeatureGate = false;
            let willChangeAbuse = 0;
            let zIndexAbuse = 0;
            let nestedOverflowHidden = 0;

            for (const sheet of document.styleSheets) {
                try {
                    const href = sheet.href ? new URL(sheet.href).pathname : '';
                    if (href.includes('/vendor/') || href.includes('bootstrap') || isLintInjectedSheet(sheet)) continue;

                    for (const rule of sheet.cssRules) {
                        if (!rule.cssText) continue;
                        const text = rule.cssText;

                        if (rule instanceof CSSSupportsRule
                            && rule.conditionText?.includes('color-mix')) {
                            hasColorMixFeatureGate = true;
                        }

                        // WebKit-only prefixes (excluding standard -webkit-overflow-scrolling)
                        const webkitMatches = text.match(/-webkit-(?!overflow-scrolling|font-smoothing|text-size-adjust|backdrop-filter|appearance|scrollbar|search-|line-clamp|box-|touch-callout)/g);
                        if (webkitMatches) webkitOnlyRules += webkitMatches.length;

                        // color-mix() without fallback
                        if (text.includes('color-mix(') && !(rule instanceof CSSSupportsRule)) {
                            colorMixTotal++;
                            // Check for fallback pattern (same property defined twice, first as solid)
                            const propMatch = text.match(/:\s*(#[0-9a-fA-F]{3,8}|rgb[^;]*|var\([^;]*\)|transparent)[;\s].*?:\s*color-mix/);
                            if (propMatch) colorMixWithFallback++;
                        }

                        // will-change abuse (not in animation/transition context)
                        if (text.includes('will-change') && !text.includes('opacity') && !text.includes('animation')) {
                            willChangeAbuse++;
                        }

                        // z-index abuse (values > 1000)
                        const zMatches = text.match(/z-index:\s*(\d+)/g);
                        if (zMatches) {
                            for (const m of zMatches) {
                                const val = parseInt(m.replace(/z-index:\s*/, ''), 10);
                                if (val > 1200) zIndexAbuse++;
                            }
                        }
                    }
                } catch { /* cross-origin */ }
            }

            // Check for nested overflow:hidden (scroll trap pattern)
            const checkOverflowNesting = (el, depth = 0) => {
                if (depth > 5) return 0;
                const style = window.getComputedStyle(el);
                const isHidden = style.overflow === 'hidden' || style.overflowY === 'hidden';
                let count = 0;
                if (isHidden) {
                    for (const child of el.children) {
                        const childStyle = window.getComputedStyle(child);
                        if (childStyle.overflow === 'hidden' || childStyle.overflowY === 'hidden') {
                            count++;
                        }
                        count += checkOverflowNesting(child, depth + 1);
                    }
                }
                return count;
            };

            const appRoot = document.querySelector('.app-root');
            if (appRoot && !document.body.classList.contains('app-root--dashboard')) {
                nestedOverflowHidden = checkOverflowNesting(appRoot);
            }

            return {
                webkitOnlyRules,
                // The design tokens define solid fallbacks and opt into
                // color-mix only inside an explicit @supports gate. CSSOM
                // serialisation drops the earlier fallback declaration, so
                // counting it from computed rules would be a false positive.
                colorMixWithoutFallback: hasColorMixFeatureGate
                    ? 0
                    : colorMixTotal - colorMixWithFallback,
                willChangeAbuse,
                zIndexAbuse,
                nestedOverflowHidden,
            };
        })();

        // iOS viewport and input hardening (2026-09-03). These are the
        // Safari-on-device behaviours that headless WebKit cannot reproduce
        // on its own, so they are checked against the CSS source instead.
        const iosViewportLint = (() => {
            // iPadOS runs the same WebKit: the collapsing toolbar, the 16px
            // input-zoom threshold and the safe-area insets all apply there too.
            if (!isTouch) {
                return {
                    iosInputZoomTargets: [],
                    viewportUnitTraps: [],
                    safeAreaInsetsDisabled: false,
                    bottomPinnedWithoutSafeArea: [],
                };
            }

            // Flatten to plain style rules so @media/@supports blocks are
            // inspected per selector instead of as one concatenated blob.
            const styleRules = [];
            const walk = (rules) => {
                for (const rule of rules) {
                    if (rule.cssRules && rule.cssRules.length) walk(rule.cssRules);
                    else if (rule.selectorText && rule.style) {
                        styleRules.push({ selector: rule.selectorText, cssText: rule.cssText });
                    }
                }
            };
            for (const sheet of document.styleSheets) {
                try {
                    const href = sheet.href ? new URL(sheet.href).pathname : '';
                    if (href.includes('/vendor/') || href.includes('bootstrap') || isLintInjectedSheet(sheet)) continue;
                    walk(sheet.cssRules);
                } catch { /* cross-origin */ }
            }
            const allCss = styleRules.map((rule) => rule.cssText).join('\n');

            // env(safe-area-inset-*) stays 0 unless the viewport meta opts into
            // the full screen, so without viewport-fit=cover every safe-area
            // declaration is dead code rather than notch protection.
            const viewportMeta = document.querySelector('meta[name="viewport"]')?.getAttribute('content') || '';
            const safeAreaInsetsDisabled = /env\(\s*safe-area-inset-/.test(allCss)
                && !/viewport-fit\s*=\s*cover/.test(viewportMeta);

            // Anything flush with the bottom edge sits under the home indicator
            // unless the stylesheet reserves safe-area-inset-bottom somewhere.
            const declaresSafeAreaBottom = /env\(\s*safe-area-inset-bottom/.test(allCss);
            const bottomPinnedWithoutSafeArea = declaresSafeAreaBottom
                ? []
                : Array.from(document.querySelectorAll('*'))
                    .filter((el) => isLayoutVisible(el))
                    .filter((el) => {
                        const style = window.getComputedStyle(el);
                        if (style.position !== 'fixed' && style.position !== 'sticky') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.height > 0 && Math.abs(rect.bottom - window.innerHeight) <= 2;
                    })
                    .map((el) => ({ tag: el.tagName.toLowerCase(), id: el.id || null }));

            // vh resolves against the largest viewport on iOS Safari, so a 100vh
            // box is taller than the visible area while the URL bar is showing.
            // CSSOM keeps only the last declaration per property, so a rule that
            // still reports vh has no dvh/svh fallback behind it.
            const viewportUnitTraps = [];
            for (const { selector, cssText } of styleRules) {
                const body = cssText.slice(cssText.indexOf('{') + 1);
                for (const property of ['height', 'min-height', 'max-height']) {
                    const pattern = new RegExp(`(?:^|[;{\\s])${property}\\s*:\\s*([^;}]+)`, 'g');
                    let match;
                    while ((match = pattern.exec(body)) !== null) {
                        const value = match[1].trim();
                        if (!/\d(?:\.\d+)?vh\b/.test(value)) continue;
                        viewportUnitTraps.push({ selector: selector.slice(0, 80), property, value });
                    }
                }
            }

            // Focusing a control below 16px makes iOS Safari zoom the page in
            // and never zoom back out. Desktop-only browsers show nothing.
            const zoomableControls = 'input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea';
            const iosInputZoomTargets = Array.from(document.querySelectorAll(zoomableControls))
                .filter((el) => isLayoutVisible(el))
                .map((el) => ({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    type: el.getAttribute('type') || null,
                    fontSize: Math.round(Number.parseFloat(window.getComputedStyle(el).fontSize) * 100) / 100,
                }))
                .filter((entry) => Number.isFinite(entry.fontSize) && entry.fontSize < 16);

            return {
                iosInputZoomTargets,
                viewportUnitTraps,
                safeAreaInsetsDisabled,
                bottomPinnedWithoutSafeArea,
            };
        })();

        const mobileTitleCellFlexRegression = (() => {
            if (!isMobile) return false;

            const titleCell = document.querySelector('td.job-title-cell[data-label="Title"]');
            if (!titleCell) return false;

            const titleText = titleCell.querySelector('.job-title-text');
            const copyButton = titleCell.querySelector('.job-copy-url-btn');
            if (!titleText || !copyButton) return false;

            const cellStyle = window.getComputedStyle(titleCell);
            const textStyle = window.getComputedStyle(titleText);
            const buttonStyle = window.getComputedStyle(copyButton);

            return cellStyle.display !== 'flex'
                || cellStyle.flexWrap !== 'nowrap'
                || textStyle.whiteSpace !== 'nowrap'
                || textStyle.overflow !== 'hidden'
                || buttonStyle.flexShrink !== '0';
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
                    const bothSmall = (a.width < touchTargetMin || a.height < touchTargetMin)
                        && (b.width < touchTargetMin || b.height < touchTargetMin);

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

        // 10. External font CDN requests (Material Symbols / Google Fonts must be self-hosted)
        const externalFontRequests = (() => {
            const hits = [];
            const entries = performance.getEntriesByType('resource');
            for (const e of entries) {
                if (e.initiatorType !== 'css' && e.initiatorType !== 'link') continue;
                try {
                    const host = new URL(e.name).hostname;
                    if (host.includes('fonts.googleapis.com') || host.includes('fonts.gstatic.com')) {
                        hits.push(e.name);
                    }
                } catch { /* malformed URL */ }
            }
            return hits;
        })();

        // 11. Material Symbols variable font: font-variation-settings must be declared
        const materialSymbolsMissingVariationSettings = (() => {
            const icons = Array.from(document.querySelectorAll('.material-symbols-outlined'));
            if (!icons.length) return false;
            const style = window.getComputedStyle(icons[0]);
            const fvs = style.getPropertyValue('font-variation-settings');
            // Must declare at least FILL and wght axes
            return !fvs || fvs === 'normal' || !fvs.includes('FILL');
        })();

        // 12. Dropdown-toggle caret on icon-only buttons (Bootstrap ::after pseudo)
        const dropdownCaretIssues = (() => {
            const issues = [];
            for (const btn of document.querySelectorAll('.btn-icon.dropdown-toggle, .action-menu-toggle.dropdown-toggle')) {
                if (!isVisible(btn)) continue;
                const before = window.getComputedStyle(btn, '::after');
                // Bootstrap caret: display != none and border-top-width > 0
                if (before.display !== 'none' && parseFloat(before.borderTopWidth) > 0) {
                    issues.push({
                        tag: btn.tagName.toLowerCase(),
                        id: btn.id || null,
                        className: typeof btn.className === 'string' ? btn.className.split(' ').filter(Boolean).slice(0, 4).join(' ') : '',
                    });
                }
            }
            return issues;
        })();

        // iOS / rendering stability checks

        // 1. Broken or zero-size icons (SVG / IMG)
        const brokenImages = Array.from(document.images)
            .filter((image) => image.complete && image.naturalWidth === 0
                && Boolean(image.currentSrc || image.getAttribute('src')))
            .map((image) => ({
                id: image.id || null,
                src: image.currentSrc || image.getAttribute('src'),
            }));

        const brokenIcons = Array.from(document.querySelectorAll('svg, img'))
            .filter(el => isVisible(el))
            .map(el => {
                const rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                };
            })
            .filter(entry => entry.width === 0 || entry.height === 0);

        // 2. SVG rendering issues (missing dimensions / inline issues)
        const svgIssues = Array.from(document.querySelectorAll('svg'))
            .filter(el => isVisible(el))
            .filter(el => {
                const hasSize = el.getAttribute('width') || el.getAttribute('height');
                const style = window.getComputedStyle(el);
                return !hasSize && style.display === 'inline';
            })
            .map(el => ({
                id: el.id || null,
                className: typeof el.className === 'string' ? el.className : '',
            }));

        // 3. Icon font fallback issues (FOIT / invisible icons)
        const iconFontIssues = Array.from(document.querySelectorAll('.material-symbols-outlined'))
            .filter(el => isVisible(el))
            .filter(el => {
                const style = window.getComputedStyle(el);
                return style.fontFamily.indexOf('Material') === -1;
            })
            .map(el => ({
                id: el.id || null,
            }));

        // 4. Horizontal scroll container missing on wide tables
        const tableOverflowIssues = Array.from(document.querySelectorAll('table'))
            .filter(el => isLayoutVisible(el))
            .filter(el => el.scrollWidth > window.innerWidth + 2)
            .filter(el => {
                let parent = el.parentElement;
                while (parent) {
                    const style = window.getComputedStyle(parent);
                    if (style.overflowX === 'auto' || style.overflowX === 'scroll') {
                        return false;
                    }
                    parent = parent.parentElement;
                }
                return true;
            })
            .map(el => ({
                id: el.id || null,
            }));

        // 5. Missing momentum scroll on iOS containers
        const supportsMomentumScroll = Boolean(
            window.CSS?.supports?.('-webkit-overflow-scrolling: touch')
        );
        const missingMomentumScroll = isTouch && supportsMomentumScroll
            ? Array.from(document.querySelectorAll('*'))
                .filter(el => isLayoutVisible(el))
                .filter(el => {
                    const style = window.getComputedStyle(el);
                    const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
                    if (!scrollable) return false;
                    return style.webkitOverflowScrolling !== 'touch';
                })
                .map(el => ({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                }))
            : [];

        // 6. Backdrop-filter without iOS fallback (runtime check)
        const backdropWithoutFallback = Array.from(document.querySelectorAll('*'))
            .filter(el => isVisible(el))
            .filter(el => {
                const style = window.getComputedStyle(el);
                if (!style.backdropFilter || style.backdropFilter === 'none') return false;

                // check if any parent disables it (iOS fallback)
                let parent = el.parentElement;
                while (parent) {
                    const ps = window.getComputedStyle(parent);
                    if (ps.backdropFilter === 'none') return false;
                    parent = parent.parentElement;
                }
                return true;
            })
            .map(el => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
            }));

        // 7. Image hidden by opacity hack (false visual stability)
        const invisibleMedia = Array.from(document.querySelectorAll('img, video'))
            .filter(el => {
                const style = window.getComputedStyle(el);
                return style.opacity === '0';
            })
            .map(el => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
            }));

        // 8. iOS background-attachment:fixed without @supports fallback.
        // Detects fixed attachment on .app-root / body without a matching scroll override.
        const backgroundAttachmentFixedWithoutFallback = (() => {
            const roots = [document.querySelector('.app-root'), document.body].filter(Boolean);
            for (const el of roots) {
                const style = window.getComputedStyle(el);
                if (style.backgroundAttachment === 'fixed') {
                    // Check if any stylesheet has a @supports (-webkit-touch-callout:none) override
                    let hasIosOverride = false;
                    for (const sheet of document.styleSheets) {
                        try {
                            for (const rule of sheet.cssRules) {
                                if (rule.type === CSSRule.SUPPORTS_RULE &&
                                    rule.conditionText && rule.conditionText.includes('-webkit-touch-callout')) {
                                    for (const inner of rule.cssRules) {
                                        if (inner.selectorText && inner.selectorText.includes('app-root')) {
                                            const val = inner.style.backgroundAttachment;
                                            if (val === 'scroll' || val === 'local') hasIosOverride = true;
                                        }
                                    }
                                }
                            }
                        } catch { /* cross-origin */ }
                    }
                    if (!hasIosOverride) return true;
                }
            }
            return false;
        })();

        // 14. Login shell layout: must use align-items:center (not flex-start which was a regression)
        const loginShellAlignmentIssue = (() => {
            const shell = document.querySelector('.login-shell');
            if (!shell) return false;
            const style = window.getComputedStyle(shell);
            return style.alignItems !== 'center';
        })();

        // Layout stability checks

        const isDashboardViewportLayout = Boolean(
            document.body.classList.contains('app-root--dashboard')
            && document.querySelector('.dashboard-layout')
            && document.querySelector('#jobsCard')
        );

        // 15. Submit card must shrink to content (height: auto, flex: 0 0 auto)
        const submitCardNotShrinking = (() => {
            const card = document.querySelector('#submitCard');
            if (!card || !isLayoutVisible(card)) return false;
            const style = window.getComputedStyle(card);
            // Must have flex-grow: 0 to prevent expansion
            return style.flexGrow !== '0' || style.height === '100%';
        })();

        // 16. Jobs card header must not expand (flex: 0 0 auto)
        const jobsCardHeaderExpanding = (() => {
            const header = document.querySelector('.jobs-card-header, #jobsCard .ui-card-header');
            if (!header || !isLayoutVisible(header)) return false;
            const style = window.getComputedStyle(header);
            // Header must have flex-grow: 0
            return style.flexGrow !== '0';
        })();

        // 17. Dashboard shell must own the viewport and delegate scrolling locally
        const dashboardViewportContractBroken = (() => {
            if (isMobile) return false;
            if (!isDashboardViewportLayout) return false;
            const shell = document.querySelector('.app-shell');
            const main = document.querySelector('.app-main');
            const layout = document.querySelector('.dashboard-layout');
            if (!shell || !main || !layout) return true;

            const viewportOwner = document.body.classList.contains('app-root--dashboard')
                ? document.body
                : shell.parentElement;
            if (!(viewportOwner instanceof HTMLElement)) return true;

            const viewportStyle = window.getComputedStyle(viewportOwner);
            const shellStyle = window.getComputedStyle(shell);
            const mainStyle = window.getComputedStyle(main);
            const shellRect = shell.getBoundingClientRect();

            const viewportLocked = ['hidden', 'clip'].includes(viewportStyle.overflow)
                || ['hidden', 'clip'].includes(viewportStyle.overflowY);
            const shellFitsViewport = Math.abs(shellRect.height - window.innerHeight) <= 4;
            const shellIsColumn = shellStyle.display.includes('flex') && shellStyle.flexDirection === 'column';
            const mainIsFlexColumn = mainStyle.display.includes('flex') && mainStyle.flexDirection === 'column';
            const hasLocalJobsScroller = Boolean(document.querySelector('#jobsCard .jobs-list-shell'));

            return !(viewportLocked && shellFitsViewport && shellIsColumn && mainIsFlexColumn && hasLocalJobsScroller);
        })();

        // 18. Jobs card must fill the remaining viewport height
        const jobsCardNotFillingHeight = (() => {
            if (isMobile) return false;
            if (!isDashboardViewportLayout) return false;
            const card = document.querySelector('#jobsCard');
            const body = document.querySelector('#jobsCard .ui-card-body');
            const scroller = document.querySelector('#jobsCard .jobs-list-shell');
            if (!card || !body || !scroller) return true;

            const cardStyle = window.getComputedStyle(card);
            const bodyStyle = window.getComputedStyle(body);
            const scrollerStyle = window.getComputedStyle(scroller);

            const fillsHeight = cardStyle.flexGrow === '1'
                && bodyStyle.flexGrow === '1'
                && scrollerStyle.flexGrow === '1';
            const shrinkable = cardStyle.minHeight === '0px'
                && bodyStyle.minHeight === '0px'
                && scrollerStyle.minHeight === '0px';

            return !(fillsHeight && shrinkable);
        })();

        // 19. Jobs list shell must be the only vertical scroll owner on desktop
        const tableResponsiveGhostScroll = (() => {
            if (isMobile) return false;
            const container = document.querySelector('#jobsCard .jobs-list-shell');
            if (!container || !isLayoutVisible(container)) return isDashboardViewportLayout;
            const style = window.getComputedStyle(container);
            const overflowYOk = style.overflowY === 'auto' || style.overflowY === 'scroll';
            const overflowXOk = style.overflowX === 'hidden' || style.overflowX === 'clip';
            const flexes = style.flexGrow === '1';
            const minHeightZero = style.minHeight === '0px' || style.minHeight === '0';

            return isDashboardViewportLayout
                && (!(overflowYOk && overflowXOk && flexes && minHeightZero) || pageScrollable);
        })();

        // 19a. Mobile dashboard must not trap scrolling inside the jobs card subtree
        const mobileJobsPageScrollTrap = (() => {
            if (!isMobile) return false;

            const card = document.querySelector('#jobsCard');
            const body = document.querySelector('#jobsCard .ui-card-body');
            const scroller = document.querySelector('#jobsCard .jobs-list-shell');
            if (!card || !body || !scroller) return false;

            const cardStyle = window.getComputedStyle(card);
            const bodyStyle = window.getComputedStyle(body);
            const scrollerStyle = window.getComputedStyle(scroller);

            return [[card, cardStyle], [body, bodyStyle], [scroller, scrollerStyle]].some(([el, style]) => {
                const trapsScroll = style.overflow === 'hidden'
                    || style.overflowY === 'hidden'
                    || style.overflowY === 'auto'
                    || style.overflowY === 'scroll';
                const actuallyScrolls = el.scrollHeight > el.clientHeight + 2;
                return trapsScroll && actuallyScrolls;
            });
        })();

        // 19b. The sentinel must live inside the local jobs scroll container
        const jobsSentinelOutsideScrollContainer = (() => {
            if (!document.querySelector('#jobsSentinel')) return false;
            const container = document.querySelector('#jobsCard .jobs-list-shell');
            const sentinel = document.querySelector('#jobsSentinel');
            if (!container || !sentinel) return true;
            return !container.contains(sentinel);
        })();

        // 20. Footer must stay pinned inside the viewport shell
        const stickyFooterDetached = (() => {
            if (isMobile) return false;
            if (!isDashboardViewportLayout) return false;
            const footer = document.querySelector('.wb-footer');
            if (!footer || !isLayoutVisible(footer)) return true;
            const style = window.getComputedStyle(footer);
            const rect = footer.getBoundingClientRect();
            const main = document.querySelector('.app-main');
            const mainRect = main?.getBoundingClientRect();
            const inFlow = style.position === 'static' || style.position === 'relative';
            const doesNotOverlapMain = !mainRect
                || rect.top >= mainRect.bottom - 4
                || rect.bottom <= mainRect.top + 4;
            const shortPage = document.documentElement.scrollHeight <= window.innerHeight + 4;
            const pinnedWhenShort = !shortPage || Math.abs(rect.bottom - window.innerHeight) <= 4;
            const stableFlexItem = style.flexGrow === '0' && style.flexShrink === '0';

            return !(inFlow && doesNotOverlapMain && pinnedWhenShort && stableFlexItem);
        })();

        // 21. Desktop jobs table header must remain sticky inside the scroller
        const stickyTableHeaderBroken = await (async () => {
            if (!isDashboardViewportLayout || isMobile) return false;
            const container = document.querySelector('#jobsCard .jobs-list-shell');
            const thead = document.querySelector('#jobsCard .table thead');
            const firstHeader = document.querySelector('#jobsCard .table thead th');
            if (!container || !thead || !firstHeader || !isLayoutVisible(firstHeader)) return true;

            const theadStyle = window.getComputedStyle(thead);
            const headerStyle = window.getComputedStyle(firstHeader);
            const stickyConfigured = (theadStyle.position === 'sticky' || headerStyle.position === 'sticky')
                && headerStyle.top === '0px';
            const hasSolidBackground = headerStyle.backgroundColor
                && headerStyle.backgroundColor !== 'rgba(0, 0, 0, 0)'
                && headerStyle.backgroundColor !== 'transparent';

            if (!stickyConfigured || !hasSolidBackground) return true;
            if (container.scrollHeight <= container.clientHeight + 2) return false;

            const previousScrollTop = container.scrollTop;
            const scrollerTop = container.getBoundingClientRect().top;
            const nextScrollTop = Math.min(previousScrollTop + 96, container.scrollHeight - container.clientHeight);

            container.scrollTop = nextScrollTop;
            await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const stickyTop = firstHeader.getBoundingClientRect().top;
            container.scrollTop = previousScrollTop;
            await new Promise((resolve) => requestAnimationFrame(resolve));

            return Math.abs(stickyTop - scrollerTop) > 4;
        })();

        // 22. Trim waveform container must have proper styling
        const trimWaveformMissingStyle = (() => {
            const wave = document.querySelector('#trimWave');
            if (!wave) return false;
            const style = window.getComputedStyle(wave);
            // Must have background, border, and border-radius
            const hasBg = style.backgroundColor && style.backgroundColor !== 'rgba(0, 0, 0, 0)';
            const hasBorder = style.borderStyle !== 'none' && style.borderWidth !== '0px';
            const hasRadius = style.borderRadius && style.borderRadius !== '0px';
            return !hasBg || !hasBorder || !hasRadius;
        })();

        const hasDeclaration = (selector, property, expected) => {
            const matchesExpected = (value) => {
                if (typeof expected === 'function') return expected(value);
                return value === expected;
            };

            const visitRules = (rules) => {
                for (const rule of rules) {
                    if (rule.cssRules?.length) {
                        if (visitRules(rule.cssRules)) return true;
                    }
                    if (!rule.selectorText || !rule.style) continue;
                    const matchesSelector = rule.selectorText
                        .split(',')
                        .map((part) => part.trim())
                        .includes(selector);
                    if (!matchesSelector) continue;
                    const value = rule.style.getPropertyValue(property)?.trim();
                    if (value && matchesExpected(value)) return true;
                }
                return false;
            };

            for (const sheet of document.styleSheets) {
                try {
                    if (visitRules(sheet.cssRules)) return true;
                } catch {
                    // Ignore cross-origin stylesheets.
                }
            }
            return false;
        };

        // 23. Video previews must remain shrink-safe and viewport-bounded.
        // Portrait thumbnails retain their intrinsic ratio, but may never use
        // more than 42% of the stable viewport height (38% on mobile).
        const videoPreviewContractBroken = (() => {
            const previewGrid = document.querySelector('#videoPreviewGrid');
            const thumb = document.querySelector('.video-thumb');
            const meta = document.querySelector('#videoMeta');
            if (!previewGrid || !thumb || !meta) return false;

            const isZero = (value) => value === '0' || value === '0px';
            const isHundredPercent = (value) => value === '100%';
            const usesPreviewHeightLimit = (value) => value === 'var(--video-preview-max-height)';
            const computedMaxHeight = Number.parseFloat(window.getComputedStyle(thumb).maxHeight);
            const viewportHeightRatio = computedMaxHeight / window.innerHeight;
            const viewportHeightLimit = isMobile ? 0.38 : 0.42;
            const hasViewportBound = Number.isFinite(viewportHeightRatio)
                && viewportHeightRatio <= viewportHeightLimit + 0.001;

            return !(
                hasDeclaration('.video-preview-grid', 'min-width', isZero)
                && hasDeclaration('.video-preview-grid > *', 'min-width', isZero)
                && hasDeclaration('.video-thumb', 'display', 'grid')
                && hasDeclaration('.video-thumb', 'place-items', 'center')
                && hasDeclaration('.video-thumb', 'align-self', 'start')
                && hasDeclaration('.video-thumb', 'width', isHundredPercent)
                && hasDeclaration('.video-thumb', 'min-width', isZero)
                && hasDeclaration('.video-thumb', 'max-width', isHundredPercent)
                && hasDeclaration('.video-thumb', 'max-height', usesPreviewHeightLimit)
                && hasDeclaration('.video-thumb', 'overflow', 'hidden')
                && hasDeclaration('.video-thumb img', 'width', 'auto')
                && hasDeclaration('.video-thumb img', 'max-width', isHundredPercent)
                && hasDeclaration('.video-thumb img', 'display', 'block')
                && hasDeclaration('.video-thumb img', 'height', 'auto')
                && hasDeclaration('.video-thumb img', 'max-height', usesPreviewHeightLimit)
                && hasDeclaration('.video-thumb img', 'object-fit', 'contain')
                && hasViewportBound
            );
        })();

        // 24. Mobile primary actions must read as raised buttons, not bare
        // glyphs. Keep the resting surface, keyboard focus, pointer hover and
        // touch press states explicit even when there are no jobs to render.
        const mobileActionSurfaceContractBroken = (() => {
            if (!document.querySelector('#jobsRenderRoot')) return false;

            const hasRaisedBackground = (value) => (
                value.includes('linear-gradient') && value.includes('var(--surface-solid)')
            );
            const hasLayeredShadow = (value) => value !== 'none' && value.includes(',');
            const transitionsShadow = (value) => value.includes('box-shadow');
            const hasRaisedDoneBackground = (value) => (
                value.includes('linear-gradient') && value.includes('var(--success)')
            );

            return !(
                hasDeclaration('.jobs-mobile-action', 'border-width', '1px')
                && hasDeclaration('.jobs-mobile-action', 'border-style', 'solid')
                && hasDeclaration('.jobs-mobile-action', 'background', hasRaisedBackground)
                && hasDeclaration('.jobs-mobile-action', 'box-shadow', hasLayeredShadow)
                && hasDeclaration('.jobs-mobile-action', 'transition', transitionsShadow)
                && hasDeclaration(
                    '.jobs-mobile-action[href^="/download/"]',
                    'box-shadow',
                    hasLayeredShadow,
                )
                && hasDeclaration('.jobs-mobile-action:focus-visible', 'outline', (value) => value !== 'none')
                && hasDeclaration('.jobs-mobile-action:active', 'transform', 'translateY(1px)')
                && hasDeclaration('.jobs-mobile-action-group', 'box-shadow', hasLayeredShadow)
                && hasDeclaration(
                    '.job-item__primary-action .jobs-mobile-action-group .jobs-mobile-action--menu',
                    'width',
                    '32px',
                )
                && hasDeclaration('#jobsCard', 'overflow', 'visible')
                && hasDeclaration(
                    '.job-item.row-done .job-item__status .status-pill-success',
                    'background',
                    hasRaisedDoneBackground,
                )
                && hasDeclaration(
                    '.job-item.row-done .job-item__status .status-pill-success',
                    'box-shadow',
                    hasLayeredShadow,
                )
            );
        })();

        // 25. Settings password stack must include shrink-safe min-width rules
        const settingsFieldStackContractBroken = (() => {
            const fieldStack = document.querySelector('.settings-field-stack');
            if (!fieldStack) return false;

            const isZero = (value) => value === '0' || value === '0px';

            return !(
                hasDeclaration('.settings-field-stack', 'min-width', isZero)
                && hasDeclaration('.settings-field-stack > *', 'min-width', isZero)
                && hasDeclaration('.settings-field-stack .d-flex', 'min-width', isZero)
            );
        })();

        // 26. Lalal actions share one equal two-column row on mobile. If the
        // disconnect action is hidden, the remaining connect action expands
        // across both tracks instead of leaving an empty half-row.
        const lalalMobileActionLayoutBroken = (() => {
            if (!isMobile) return false;

            const row = document.querySelector('.lalal-tile-action-row');
            const authButton = document.querySelector('#lalalAuthBtn');
            const disconnectButton = document.querySelector('#lalalDisconnectBtn');
            if (!(row instanceof HTMLElement)
                || !(authButton instanceof HTMLElement)
                || !(disconnectButton instanceof HTMLElement)) {
                return false;
            }

            const isEqualTwoColumnGrid = (value) => (
                /^repeat\(2, minmax\(0(?:px)?, 1fr\)\)$/.test(value.replace(/\s+/g, ' '))
            );
            const isFullWidth = (value) => value === '100%';
            const isZero = (value) => value === '0' || value === '0px';

            return !(
                hasDeclaration('.lalal-tile-action-row', 'display', 'grid')
                && hasDeclaration(
                    '.lalal-tile-action-row',
                    'grid-template-columns',
                    isEqualTwoColumnGrid,
                )
                && hasDeclaration('.lalal-tile-action-row', 'width', isFullWidth)
                && hasDeclaration('.lalal-tile-action-row > .btn', 'width', isFullWidth)
                && hasDeclaration('.lalal-tile-action-row > .btn', 'min-width', isZero)
                && hasDeclaration(
                    '.lalal-tile-action-row:has(#lalalDisconnectBtn.d-none) #lalalAuthBtn',
                    'grid-column',
                    '1 / -1',
                )
            );
        })();

        // 27. Trim modal should open with a small selection at the track start.
        const trimDefaultSelectionInvalid = (() => {
            const trimModal = document.querySelector('#trimModal');
            if (!trimModal || !trimModal.classList.contains('show')) return false;
            const regions = document.querySelectorAll('#trimWave [data-id], #trimWave .wavesurfer-region');
            const infoText = document.querySelector('#trimInfo');
            const text = infoText?.textContent?.trim() || '';
            return regions.length === 0 || !text.startsWith('0:00.00');
        })();

        // 28. UI card children should not expand unless explicitly needed
        const uiCardChildExpands = (() => {
            let count = 0;
            // Check generic .ui-card > div children (not header/body)
            const cards = document.querySelectorAll('.ui-card:not(.ui-card--stat)');
            for (const card of cards) {
                if (!isLayoutVisible(card)) continue;
                const children = Array.from(card.children).filter(
                    c => c.tagName === 'DIV' &&
                        !c.classList.contains('ui-card-header') &&
                        !c.classList.contains('ui-card-body')
                );
                for (const child of children) {
                    const style = window.getComputedStyle(child);
                    // Check if #submitCard's inner div has flex-grow: 0
                    if (card.id === 'submitCard' && style.flexGrow !== '0') {
                        count++;
                    }
                }
            }
            return count;
        })();

        // Trim modal stability checks
        const trimModal = document.querySelector('#trimModal');
        let trimInvalidRanges = false;
        let trimSnapViolations = 0;
        let trimLoopDriftMs = 0;
        let trimZoomInstability = 0;
        let trimHandleTooSmall = 0;
        const trimKeyboardMissing = [];
        const trimKeyboardConflicts = [];
        let trimInstanceLeaks = 0;
        let trimUiAudioMismatchMs = 0;

        if (trimModal && trimModal.classList.contains('show')) {
            const SNAP = 0.5;

            const start = window.__trimStart;
            const end = window.__trimEnd;
            const duration = window.__trimDuration;

            if (
                !Number.isFinite(start) ||
                !Number.isFinite(end) ||
                !Number.isFinite(duration) ||
                start < 0 ||
                end > duration ||
                start >= end
            ) {
                trimInvalidRanges = true;
            }

            const isSnapped = (value) => Math.abs(value / SNAP - Math.round(value / SNAP)) < 0.001;
            if (Number.isFinite(start) && !isSnapped(start)) trimSnapViolations++;
            if (Number.isFinite(end) && !isSnapped(end)) trimSnapViolations++;

            const handles = document.querySelectorAll('.trim-handle');
            for (const handle of handles) {
                const rect = handle.getBoundingClientRect();
                if (rect.width < 24 || rect.height < 24) {
                    trimHandleTooSmall++;
                }
            }

            if (window.__wavesurferInstances && window.__wavesurferInstances.length > 1) {
                trimInstanceLeaks = window.__wavesurferInstances.length;
            }

            if (window.__wavesurfer && Number.isFinite(start)) {
                try {
                    const current = window.__wavesurfer.getCurrentTime();
                    trimUiAudioMismatchMs = Math.abs(current - start) * 1000;
                } catch {
                    // ignore runtime probe failures
                }
            }

            const wave = document.querySelector('#trimWave');
            if (wave) {
                const rect1 = wave.getBoundingClientRect();
                await new Promise((resolve) => requestAnimationFrame(resolve));
                const rect2 = wave.getBoundingClientRect();
                if (Math.abs(rect1.width - rect2.width) > 2) {
                    trimZoomInstability++;
                }
            }

            if (window.__trimLoopDriftMs) {
                trimLoopDriftMs = window.__trimLoopDriftMs;
            }
        }

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
            footerSpaceReservationMissing,
            footerViewportPlacementBroken,
            navbarButtonAlignment,
            mutationObservers,
            duplicateEventHandlers,
            // New CSS review checks
            undefinedCustomProperties,
            backdropFilterCount,
            stickyWithoutBackground,
            focusVisibleCheck,
            unguardedAnimations,
            clippedDropdowns,
            jobsActionRightEdgeSpacing,
            escapedHtmlElements,
            importantAbuse,
            tightlyPackedTargets,
            localOverflowIssues,
            focusIndicatorMissing,
            ghostScrollContainers,
            nestedScrollContainers,
            flexScrollTraps,
            doubleScrollRisk,
            viewportScrollLeak,
            overflowHiddenScrollBlockers,
            flexMinHeightOverflowHidden,
            hasSelectorLayoutUsage,
            viewportLockingIssues,
            badgeInconsistencies,
            platformBadgeGeometryIssues,
            iconPointerEventsIssues,
            gridViolations,
            overlapIssues,
            brokenTitleTruncation,
            mobileJobsFeedIssues,
            tabletLayoutIssues,
            mixedLayoutIssues,
            containerWidthIssue,
            statTileMobileLayoutIssues,
            fontLoadingStatus,
            statCardCenteringIssues,
            externalFontRequests,
            materialSymbolsMissingVariationSettings,
            dropdownCaretIssues,
            brokenImages,
            brokenIcons,
            svgIssues,
            iconFontIssues,
            tableOverflowIssues,
            missingMomentumScroll,
            backdropWithoutFallback,
            invisibleMedia,
            backgroundAttachmentFixedWithoutFallback,
            loginShellAlignmentIssue,
            // Layout stability (2026-04-28)
            submitCardNotShrinking,
            jobsCardHeaderExpanding,
            tableResponsiveGhostScroll,
            jobsSentinelOutsideScrollContainer,
            dashboardViewportContractBroken,
            jobsCardNotFillingHeight,
            stickyFooterDetached,
            stickyTableHeaderBroken,
            trimWaveformMissingStyle,
            videoPreviewContractBroken,
            settingsFieldStackContractBroken,
            lalalMobileActionLayoutBroken,
            trimDefaultSelectionInvalid,
            uiCardChildExpands,
            trimInvalidRanges,
            trimSnapViolations,
            trimLoopDriftMs,
            trimZoomInstability,
            trimHandleTooSmall,
            trimKeyboardMissing,
            trimKeyboardConflicts,
            trimInstanceLeaks,
            trimUiAudioMismatchMs,
            // CSS hardening (2026-04-29)
            webkitOnlyRules: cssAdvancedLint.webkitOnlyRules,
            colorMixWithoutFallback: cssAdvancedLint.colorMixWithoutFallback,
            willChangeAbuse: cssAdvancedLint.willChangeAbuse,
            zIndexAbuse: cssAdvancedLint.zIndexAbuse,
            nestedOverflowHidden: cssAdvancedLint.nestedOverflowHidden,
            mobileTitleCellFlexRegression,
            mobileActionSurfaceContractBroken,
            mobileJobsPageScrollTrap,
            // iOS viewport hardening (2026-09-03)
            iosInputZoomTargets: iosViewportLint.iosInputZoomTargets,
            viewportUnitTraps: iosViewportLint.viewportUnitTraps,
            safeAreaInsetsDisabled: iosViewportLint.safeAreaInsetsDisabled,
            bottomPinnedWithoutSafeArea: iosViewportLint.bottomPinnedWithoutSafeArea,
        };
    }, {
        requiredSelectors: view.requiredSelectors,
        mobileTouchTargetMin: MOBILE_TOUCH_TARGET_MIN,
        desktopTouchTargetMin: DESKTOP_TOUCH_TARGET_MIN,
        // isMobile means "compact layout" - the viewport is inside
        // `@media (max-width: 1024px)`, so the jobs feed renders instead of the
        // desktop table. isTouch means "finger input". An iPad Pro in landscape
        // is the second without being the first, which is why they are separate.
        isMobile: profileIsCompactLayout(view.device),
        // Phone-viewport pixel contracts (edge-to-edge shell, the four stat
        // tiles in one row) are written against 390px and do not describe an
        // iPad, which has its own rules in the 576-1024px band.
        isPhone: deviceProfile(view.device).formFactor === 'phone',
        isTouch: profileHasTouch(view.device),
        formFactor: deviceProfile(view.device).formFactor,
        auditPlatformBadges: [...DASHBOARD_VIEW_NAMES, ...JOB_DETAIL_VIEW_NAMES].includes(view.name),
        platformBadgeWidth: PLATFORM_BADGE_WIDTH_PX,
        platformBadgeHeight: PLATFORM_BADGE_HEIGHT_PX,
        platformBadgeIconSize: PLATFORM_BADGE_ICON_SIZE_PX,
        platformBadgeGeometryEpsilon: PLATFORM_BADGE_GEOMETRY_EPSILON_PX,
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
    if (VISUAL_STABILITY_CSS) {
        const style = await page.addStyleTag({ content: VISUAL_STABILITY_CSS });
        await style.evaluate((element) => {
            element.dataset.uiLintInjected = 'true';
        }).catch(() => { });
    }
    if (profileHasTouch(view.device) && MOBILE_VISUAL_STABILITY_CSS) {
        const style = await page.addStyleTag({ content: MOBILE_VISUAL_STABILITY_CSS });
        await style.evaluate((element) => {
            element.dataset.uiLintInjected = 'true';
        }).catch(() => { });
    }
    await waitForMedia(page);
    await waitForLayoutStability(page, view);
    await page.waitForTimeout(SCREENSHOT_SETTLE_MS);
}

/**
 * Exercises the dashboard history toggle and verifies the footer contract in
 * both layout states. This is mandatory because the original regression only
 * appeared after opening and closing Job History.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<{footerHistoryTransitionBroken: boolean, footerHistoryTransitionStates: object[]}>}
 */
async function auditFooterHistoryToggle(page, view) {
    if (!DASHBOARD_VIEW_NAMES.includes(view.name)) {
        return {
            footerHistoryTransitionBroken: false,
            footerHistoryTransitionStates: [],
        };
    }

    const toggle = page.locator('#showJobHistoryToggle');
    if (await toggle.count() === 0) {
        return {
            footerHistoryTransitionBroken: true,
            footerHistoryTransitionStates: [{ state: 'missing-toggle' }],
        };
    }

    const initialChecked = await toggle.isChecked().catch(() => false);
    const states = [];
    const setChecked = async (checked) => {
        await page.evaluate((nextChecked) => {
            const input = document.querySelector('#showJobHistoryToggle');
            if (input instanceof HTMLInputElement && input.checked !== nextChecked) {
                input.click();
            }
        }, checked);
        await page.waitForTimeout(250);
    };
    const measure = async (state) => page.evaluate((currentState) => {
        const footer = document.querySelector('.wb-footer');
        const main = document.querySelector('.app-main');
        if (!footer || !main) return { state: currentState, missing: true };

        const footerStyle = window.getComputedStyle(footer);
        const mainStyle = window.getComputedStyle(main);
        const footerRect = footer.getBoundingClientRect();
        const mainRect = main.getBoundingClientRect();
        const inFlow = footerStyle.position === 'static' || footerStyle.position === 'relative';
        const shortPage = document.documentElement.scrollHeight <= window.innerHeight + 4;
        const explicitPadding = Number.parseFloat(mainStyle.paddingBottom || '0') >= footerRect.height - 4;
        const reserved = inFlow
            ? footerRect.top >= mainRect.bottom - 4
            : explicitPadding;
        const pinnedWhenShort = !shortPage
            || Math.abs(footerRect.bottom - window.innerHeight) <= 4;

        return {
            state: currentState,
            missing: false,
            inFlow,
            reserved,
            pinnedWhenShort,
            overlap: footerRect.top < mainRect.bottom - 4,
            footerTop: Math.round(footerRect.top),
            footerBottom: Math.round(footerRect.bottom),
            mainBottom: Math.round(mainRect.bottom),
            documentHeight: document.documentElement.scrollHeight,
        };
    }, state);

    try {
        await setChecked(false);
        states.push(await measure('collapsed'));
        await setChecked(true);
        states.push(await measure('expanded'));
        await setChecked(false);
        states.push(await measure('collapsed-again'));
    } finally {
        await setChecked(initialChecked);
    }

    const broken = states.some((state) => state.missing
        || !state.inFlow
        || !state.reserved
        || !state.pinnedWhenShort
        || state.overlap);
    return {
        footerHistoryTransitionBroken: broken,
        footerHistoryTransitionStates: states,
    };
}

/**
 * A non-interactive placeholder row (e.g. the "No downloads yet" empty
 * state) must not visually react to mouse hover the way a real, clickable
 * job row does - that reads as a broken/ghost interactive element. Uses a
 * real Playwright hover so the browser actually enters the :hover state,
 * since that cannot be simulated from inside page.evaluate.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<{emptyStateHoverHighlight: boolean}>}
 */
async function auditEmptyStateHover(page, view) {
    if (view.name !== 'dashboard') {
        return { emptyStateHoverHighlight: false };
    }

    const emptyRow = page.locator('#jobsTable tbody tr#emptyRow');
    if (await emptyRow.count() === 0) {
        // The dashboard had jobs seeded for this run; the empty state never
        // rendered, so there is nothing to audit.
        return { emptyStateHoverHighlight: false };
    }

    const readBackground = () => page.evaluate(() => {
        const row = document.querySelector('#jobsTable tbody tr#emptyRow');
        return row ? window.getComputedStyle(row).backgroundColor : null;
    });

    const before = await readBackground();
    await emptyRow.hover();
    await page.waitForTimeout(50);
    const after = await readBackground();
    await page.mouse.move(0, 0);

    return { emptyStateHoverHighlight: Boolean(before) && before !== after };
}

/**
 * Clicks through every settings tab and measures the vertical gap between
 * each panel's title and the content block directly beneath it. The four
 * tabs (General, Integrations, Security, System) must render this gap
 * identically - a per-tab DOM nesting difference (title as a direct child
 * of .settings-group vs. nested inside .settings-grid-item) previously let
 * conflicting !important margin-bottom overrides apply to only some tabs,
 * so the same visual gap silently drifted between 12px, 16px, and 24px.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<{settingsTabTitleGapInconsistent: boolean, settingsTabTitleGaps: object}>}
 */
async function auditSettingsTabTitleGap(page, view) {
    if (!SETTINGS_VIEW_NAMES.includes(view.name)) {
        return { settingsTabTitleGapInconsistent: false, settingsTabTitleGaps: {} };
    }

    const tabs = [
        { tabId: 'settingsGeneralTab', panelId: 'settingsGeneralPanel' },
        { tabId: 'settingsIntegrationsTab', panelId: 'settingsIntegrationsPanel' },
        { tabId: 'settingsSecurityTab', panelId: 'settingsSecurityPanel' },
        { tabId: 'settingsSystemTab', panelId: 'settingsSystemPanel' },
    ];

    const gaps = {};
    for (const { tabId, panelId } of tabs) {
        const tabButton = page.locator(`#${tabId}`);
        if (await tabButton.count() === 0) continue;
        await tabButton.click();
        await page.waitForTimeout(350);

         
        const gap = await page.evaluate((id) => {
            const panel = document.getElementById(id);
            if (!panel) return null;
            const title = panel.querySelector('h2, .settings-panel-title');
            if (!title) return null;
            const titleRect = title.getBoundingClientRect();
            // Skip siblings that render nothing -- the System tab's job-stat
            // row is display:none above 768px, and measuring its zeroed rect
            // would report a nonsense gap instead of the visible one.
            let next = title.nextElementSibling;
            while (next && next.getClientRects().length === 0) next = next.nextElementSibling;
            if (!next) return null;
            const nextRect = next.getBoundingClientRect();
            return Math.round((nextRect.top - titleRect.bottom) * 100) / 100;
        }, panelId);

        if (gap !== null) gaps[tabId] = gap;
    }

    const values = Object.values(gaps);
    const settingsTabTitleGapInconsistent = values.length > 1
        && Math.max(...values) - Math.min(...values) > 1;

    return { settingsTabTitleGapInconsistent, settingsTabTitleGaps: gaps };
}

/**
 * Opens the first real trim trigger and exercises keyboard behavior with
 * Playwright input. The dashboard may legitimately have no audio jobs, in
 * which case the dynamic audit is reported as skipped.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<{failures: string[], warnings: string[], metrics: object}>}
 */
async function auditTrimModal(page, view) {
    if (!DASHBOARD_VIEW_NAMES.includes(view.name)) {
        return { failures: [], warnings: [], metrics: {} };
    }

    const actionSurface = profileIsCompactLayout(view.device) ? '#jobsMobileList' : '#jobsTable';
    const trigger = page.locator(`${actionSurface} [data-action="open-trim"]`).first();
    try {
        await trigger.waitFor({ state: 'attached', timeout: 3000 });
    } catch {
        return {
            failures: [],
            warnings: ['trim audit skipped: no audio trim trigger is rendered'],
            metrics: {},
        };
    }

    // Finished audio jobs live in the Job History section, which is collapsed
    // by default on mobile. The trigger is then attached but zero-sized, so the
    // dropdown click would time out. Expand history first and restore it after,
    // otherwise the screenshot pair no longer matches the view's normal state.
    const historyToggle = page.locator('#showJobHistoryToggle');
    let historyExpandedByAudit = false;
    if (!await trigger.isVisible().catch(() => false) && await historyToggle.count() > 0) {
        historyExpandedByAudit = await page.evaluate(() => {
            const input = document.querySelector('#showJobHistoryToggle');
            if (!(input instanceof HTMLInputElement) || input.checked) return false;
            input.click();
            return true;
        }).catch(() => false);
        if (historyExpandedByAudit) {
            await page.waitForTimeout(400);
        }
    }

    const restoreHistory = async () => {
        if (!historyExpandedByAudit) return;
        await page.evaluate(() => {
            const input = document.querySelector('#showJobHistoryToggle');
            if (input instanceof HTMLInputElement && input.checked) input.click();
        }).catch(() => { });
        await page.waitForTimeout(400);
    };

    let trimKeyboardMissing = [];
    let trimProbeInstalled = false;
    try {
        const group = trigger.locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " btn-group ")][1]');
        await group.locator('.dropdown-toggle').first().click();
        await trigger.click();
        await page.locator('#trimModal.show').waitFor({ state: 'visible', timeout: 10000 });
        await page.locator('#trimLoader.d-none').waitFor({ state: 'attached', timeout: 15000 });

        await page.locator('#trimModal').focus();
        await page.evaluate(() => {
            window.__uiLintTrimKeyEvents = [];
            window.__uiLintTrimKeyProbe = (event) => {
                if ([' ', 'ArrowLeft', 'ArrowRight', 'l', 'L'].includes(event.key)) {
                    window.__uiLintTrimKeyEvents.push({
                        key: event.key,
                        defaultPrevented: event.defaultPrevented,
                    });
                }
            };
            document.addEventListener('keydown', window.__uiLintTrimKeyProbe);
        });
        trimProbeInstalled = true;

        const beforeLoop = await page.locator('#trimLoop').getAttribute('aria-pressed');
        await page.keyboard.press('l');
        const afterLoop = await page.locator('#trimLoop').getAttribute('aria-pressed');
        if (beforeLoop === afterLoop) {
            const keyEvents = await page.evaluate(() => window.__uiLintTrimKeyEvents);
            if (!keyEvents.some(({ key }) => key === 'l' || key === 'L')) {
                trimKeyboardMissing = ['l'];
            }
        }
        if (beforeLoop !== afterLoop) {
            await page.keyboard.press('l');
        }

        await page.keyboard.press('ArrowRight');
        await page.keyboard.press('ArrowLeft');
        const arrowEvents = await page.evaluate(() => window.__uiLintTrimKeyEvents);
        for (const key of ['ArrowRight', 'ArrowLeft']) {
            if (!arrowEvents.some(({ key: eventKey }) => eventKey === key)) {
                trimKeyboardMissing.push(key);
            }
        }

        const beforeScroll = await page.evaluate(() => window.scrollY);
        await page.keyboard.press(' ');
        const afterScroll = await page.evaluate(() => window.scrollY);
        const keyState = await page.evaluate(() => ({
            events: window.__uiLintTrimKeyEvents,
            spacePrevented: window.__uiLintTrimKeyEvents
                .filter(({ key }) => key === ' ')
                .at(-1)?.defaultPrevented ?? false,
        }));

        await page.evaluate(() => {
            if (window.__uiLintTrimKeyProbe) {
                document.removeEventListener('keydown', window.__uiLintTrimKeyProbe);
            }
            delete window.__uiLintTrimKeyProbe;
            delete window.__uiLintTrimKeyEvents;
        });

        const trimDefaultSelectionInvalid = await page.evaluate(() => {
            const infoText = document.querySelector('#trimInfo')?.textContent?.trim() || '';
            return document.querySelectorAll('#trimWave [data-id], #trimWave .wavesurfer-region').length === 0
                || !infoText.startsWith('0:00.00');
        });

        const trimKeyboardConflicts = [];
        if (!keyState.events.some(({ key }) => key === ' ')) {
            trimKeyboardMissing.push(' ');
        }
        if (!keyState.spacePrevented || afterScroll !== beforeScroll) {
            trimKeyboardConflicts.push('space-scroll');
        }

        await page.locator('#trimModal .btn-close').click().catch(() => { });
        await page.locator('#trimModal.show').waitFor({ state: 'hidden', timeout: 5000 }).catch(() => { });
        await restoreHistory();
        return {
            failures: [],
            warnings: [],
            metrics: {
                trimDefaultSelectionInvalid,
                trimKeyboardMissing: trimKeyboardMissing || [],
                trimKeyboardConflicts,
            },
        };
    } catch (error) {
        if (trimProbeInstalled) {
            await page.evaluate(() => {
                if (window.__uiLintTrimKeyProbe) {
                    document.removeEventListener('keydown', window.__uiLintTrimKeyProbe);
                }
                delete window.__uiLintTrimKeyProbe;
                delete window.__uiLintTrimKeyEvents;
            }).catch(() => { });
        }
        await page.locator('#trimModal .btn-close').click().catch(() => { });
        await restoreHistory();
        return {
            failures: [`trim modal interaction failed: ${error instanceof Error ? error.message : String(error)}`],
            warnings: [],
            metrics: {},
        };
    }
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
 * @param {'desktop'|'mobile'} [device]
 * @returns {Promise<object>}
 */
/** Result name the invalid-login check reports under, for a device profile. */
function invalidLoginResultName(device) {
    return device === 'desktop' ? 'login-error' : `${device}-login-error`;
}

async function runInvalidLoginCheck(browser, loginRequired, device = 'desktop') {
    const resultName = invalidLoginResultName(device);

    if (!loginRequired) {
        return buildSkippedResult(
            resultName,
            `${BASE_URL}/login`,
            'login is disabled; skipped invalid-login check',
        );
    }

    const context = await browser.newContext(createContextOptions(device));
    const page = await context.newPage();
    const stopCollecting = collectConsoleAndNetwork(page);

    try {
        await openView(page, loginViewFor(device));
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
        // The 401 the rejected credentials produce is the expected outcome of
        // this check, not a finding; everything else goes through the same
        // allowlist as the regular views.
        const consoleTriage = triageConsoleEntries(
            traffic.consoleEntries.filter((entry) => !/\b401\b|unauthorized/i.test(entry.text)),
        );
        warnings.push(...consoleTriage.entries.map((entry) => `console ${entry.type}: ${entry.text}`));
        warnings.push(...externalWarnings);

        return {
            name: resultName,
            url: `${BASE_URL}/login`,
            device,
            engine: deviceProfile(device).engine,
            failures,
            warnings,
            metrics: {
                loginError: metrics,
                consoleSeverityScore: consoleTriage.score,
                consoleSuppressed: consoleTriage.suppressed,
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
    // Must precede newPage(): the observer is an init script and has to be
    // registered before the document starts executing, or the shifts that
    // happen during first render are never seen.
    await installLayoutShiftObserver(context);
    const page = await context.newPage();
    const stopCollecting = collectConsoleAndNetwork(page);

    try {
        await openView(page, view, replacements);

        const runtimeMetrics = await collectMetrics(page, view);
        const footerHistoryAudit = await auditFooterHistoryToggle(page, view);
        const trimAudit = await auditTrimModal(page, view);
        const emptyStateHoverAudit = await auditEmptyStateHover(page, view);
        const settingsTabTitleGapAudit = await auditSettingsTabTitleGap(page, view);
        const jobsSourceContracts = viewAuditsJobsList(view)
            ? await getJobsSourceContractMetrics()
            : {
                jobsInfiniteScrollNotObserverBased: false,
                jobsPagingOffsetContractBroken: false,
                jobsDesktopFileSizePlacementBroken: false,
                jobsMobileShareActionMissing: false,
            };
        const settingsSourceContracts = viewAuditsSettings(view)
            ? await getSettingsSourceContractMetrics()
            : {
                settingsSaveToastContractBroken: false,
                settingsHintContractBroken: false,
                settingsHintSpacingContractBroken: false,
            };
        const metrics = {
            ...runtimeMetrics,
            ...footerHistoryAudit,
            ...trimAudit.metrics,
            ...emptyStateHoverAudit,
            ...settingsTabTitleGapAudit,
            ...jobsSourceContracts,
            ...settingsSourceContracts,
        };
        // Runs against the settled DOM, before the screenshot pair perturbs it
        // with the motion-reset stylesheet.
        const axe = RUN_AXE
            ? await runAxeAudit(page)
            : { available: false, error: 'disabled via UI_LINT_AXE=0' };
        const layoutShift = await collectLayoutShift(page);

        const shots = await captureStablePair(page, view);
        const visual = diffScreenshots({
            name: view.name,
            shotA: shots.shotA,
            shotB: shots.shotB,
            screenshotDir: SCREENSHOT_DIR,
        });
        const traffic = stopCollecting();
        const consoleTriage = triageConsoleEntries(traffic.consoleEntries);

        const failures = [];
        const warnings = [];
        const { sameOriginFailures, externalWarnings } = splitNetworkFindings(traffic);
        failures.push(...trimAudit.failures);
        warnings.push(...trimAudit.warnings);

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
        if (metrics.footerNotFlex) failures.push('footer is not using the mandatory flex layout');
        if (metrics.footerSpaceReservationMissing) {
            failures.push('footer space reservation is missing or does not cover the footer');
        }
        if (metrics.footerViewportPlacementBroken) {
            failures.push('short-page footer is not pinned to the viewport bottom');
        }
        if (metrics.footerHistoryTransitionBroken) {
            failures.push('footer contract breaks when Job History is toggled');
        }
        if (metrics.navbarButtonAlignment > 0) warnings.push(`navbar buttons not vertically centred: ${metrics.navbarButtonAlignment}`);
        // One observer is the app's intentional dynamic-jobs initializer;
        // flag only unexpected multiple observers.
        if (metrics.mutationObservers > 1) warnings.push('mutation observer usage detected');
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
        if (metrics.jobsActionRightEdgeSpacing?.length) {
            failures.push(`jobs action buttons too close to the right edge: ${metrics.jobsActionRightEdgeSpacing.length}`);
        }
        if (metrics.escapedHtmlElements?.length) {
            failures.push(`escaped HTML detected: ${metrics.escapedHtmlElements.length}`);
        }
        if (metrics.importantAbuse?.count > 10) {
            warnings.push(`excessive !important usage: ${metrics.importantAbuse.count}`);
        }
        if (metrics.tightlyPackedTargets?.length) {
            warnings.push(`tightly packed touch targets: ${metrics.tightlyPackedTargets.length}`);
        }
        if (metrics.viewportScrollLeak) {
            warnings.push('page scroll leaks past local jobs scroller');
        }
        if (metrics.localOverflowIssues?.length) {
            failures.push(`local overflow elements: ${metrics.localOverflowIssues.length}`);
        }
        if (metrics.brokenTitleTruncation?.length) {
            failures.push(`broken title truncation: ${metrics.brokenTitleTruncation.length}`);
        }
        if (metrics.tabletLayoutIssues?.length) {
            const kinds = metrics.tabletLayoutIssues.map((issue) => issue.type).join(', ');
            failures.push(`tablet layout issues (768-1024px band): ${kinds}`);
        }
        if (metrics.mobileJobsFeedIssues?.length) {
            failures.push(`mobile jobs feed layout issues: ${metrics.mobileJobsFeedIssues.length}`);
        }
        if (metrics.statCardCenteringIssues?.length) {
            failures.push(`stat card vertical centering broken (child expands): ${metrics.statCardCenteringIssues.length}`);
        }
        if (metrics.mixedLayoutIssues?.length) {
            failures.push(`mixed Bootstrap/grid layout: ${metrics.mixedLayoutIssues.length}`);
        }
        if (metrics.containerWidthIssue) {
            failures.push('app shell container does not fill the mobile viewport');
        }
        if (metrics.statTileMobileLayoutIssues?.length) {
            failures.push(`stat tile mobile layout broken (row, centering, or vertical spacing): ${metrics.statTileMobileLayoutIssues.length}`);
        }
        if (metrics.externalFontRequests.length > 0) {
            failures.push(`external font CDN requests (must be self-hosted): ${metrics.externalFontRequests.length}`);
        }
        if (metrics.materialSymbolsMissingVariationSettings) {
            warnings.push('Material Symbols Outlined missing font-variation-settings (iOS variable font rendering broken)');
        }
        if (metrics.dropdownCaretIssues.length > 0) {
            warnings.push(`icon-only dropdown buttons with visible Bootstrap caret: ${metrics.dropdownCaretIssues.length}`);
        }
        if (metrics.brokenImages.length) {
            failures.push(`broken images: ${metrics.brokenImages.length}`);
        }
        if (metrics.brokenIcons?.length) {
            failures.push(`broken or zero-size icons: ${metrics.brokenIcons.length}`);
        }
        if (metrics.svgIssues?.length) {
            warnings.push(`SVG rendering issues: ${metrics.svgIssues.length}`);
        }
        if (metrics.iconFontIssues?.length) {
            failures.push(`icon font fallback issues: ${metrics.iconFontIssues.length}`);
        }
        if (metrics.tableOverflowIssues?.length) {
            failures.push(`tables without horizontal scroll container: ${metrics.tableOverflowIssues.length}`);
        }
        if (metrics.missingMomentumScroll?.length > 3) {
            warnings.push(`missing iOS momentum scroll on containers: ${metrics.missingMomentumScroll.length}`);
        }
        if (metrics.backdropWithoutFallback?.length) {
            warnings.push(`backdrop-filter without iOS-safe fallback: ${metrics.backdropWithoutFallback.length}`);
        }
        if (metrics.invisibleMedia?.length) {
            warnings.push(`media elements hidden via opacity (may break layout tests): ${metrics.invisibleMedia.length}`);
        }
        if (metrics.backgroundAttachmentFixedWithoutFallback) {
            failures.push('background-attachment:fixed used without @supports (-webkit-touch-callout:none) scroll fallback (iOS repaint bug)');
        }
        if (metrics.loginShellAlignmentIssue) {
            failures.push('login-shell align-items is not center (layout regression)');
        }
        if (metrics.submitCardNotShrinking) {
            failures.push('dashboard submit card expands instead of shrinking to content');
        }
        if (metrics.jobsCardHeaderExpanding) {
            failures.push('jobs card header expands instead of remaining fixed-height');
        }
        if (metrics.dashboardViewportContractBroken) {
            failures.push('dashboard shell is not pinned to the viewport');
        }
        if (metrics.jobsCardNotFillingHeight) {
            failures.push('jobs card does not fill the remaining dashboard height');
        }
        if (metrics.tableResponsiveGhostScroll) {
            failures.push('jobs table scroller is misconfigured or page scroll is leaking');
        }
        if (metrics.mobileJobsPageScrollTrap) {
            failures.push('mobile jobs card still traps vertical scrolling inside nested containers');
        }
        if (metrics.flexMinHeightOverflowHidden?.length) {
            failures.push(`flex containers using min-height:0 with overflow:hidden: ${metrics.flexMinHeightOverflowHidden.length}`);
        }
        if (metrics.stickyFooterDetached) {
            failures.push('dashboard footer is outside the viewport');
        }
        if (metrics.stickyTableHeaderBroken) {
            failures.push('jobs table header is not sticky inside the scroll container');
        }
        if (metrics.jobsSentinelOutsideScrollContainer) {
            failures.push('jobs sentinel is missing from the local scroll container');
        }
        if (metrics.jobsInfiniteScrollNotObserverBased) {
            failures.push('jobs infinite scroll is not driven by an IntersectionObserver rooted at the local scroller');
        }
        if (metrics.jobsPagingOffsetContractBroken) {
            failures.push('jobs pagination offset is not monotonic across row trimming');
        }
        if (metrics.jobsDesktopFileSizePlacementBroken) {
            failures.push('desktop job file size must share the Media metadata line and stay out of Status');
        }
        if (metrics.jobsMobileShareActionMissing) {
            failures.push('mobile downloadable jobs must expose the shared Download and Share menu');
        }
        if (metrics.trimWaveformMissingStyle) {
            failures.push('trim waveform container styling is incomplete');
        }
        if (metrics.videoPreviewContractBroken) {
            failures.push('video preview is missing shrink-safe, viewport-bounded thumbnail rules');
        }
        if (metrics.settingsFieldStackContractBroken) {
            failures.push('settings field stack is missing shrink-safe flex min-width rules');
        }
        if (metrics.settingsSaveToastContractBroken) {
            failures.push('settings autosave must stay quiet, while errors use toasts and no inline save-status row is rendered');
        }
        if (metrics.settingsHintContractBroken) {
            failures.push('settings explanation hints must use the info-icon hint style');
        }
        if (metrics.settingsHintSpacingContractBroken) {
            failures.push('settings explanation hints must share the global 4px spacing rule');
        }
        if (metrics.lalalMobileActionLayoutBroken) {
            failures.push('Lalal mobile actions must use one equal two-column row');
        }
        if (metrics.trimDefaultSelectionInvalid) {
            failures.push('trim modal must open with a selection at the track start');
        }
        if (metrics.uiCardChildExpands) {
            failures.push(`ui-card children expanding unexpectedly: ${metrics.uiCardChildExpands}`);
        }
        if (metrics.emptyStateHoverHighlight) {
            failures.push('empty jobs table row highlights on hover like an interactive row');
        }
        if (metrics.settingsTabTitleGapInconsistent) {
            failures.push(`settings tab title-to-content gap is inconsistent across tabs: ${JSON.stringify(metrics.settingsTabTitleGaps)}`);
        }
        applyMetricRules(metrics, failures, warnings);
        applyAxeRules(axe, failures, warnings);

        // Only reported where the engine can actually observe shifts; see
        // lib/layout-shift.mjs for why an unsupported engine is not a zero.
        const layoutShiftRating = classifyLayoutShift(layoutShift);
        if (layoutShiftRating === 'poor') {
            failures.push(`cumulative layout shift ${layoutShift.value.toFixed(3)} (poor, > ${LAYOUT_SHIFT_POOR})`);
        } else if (layoutShiftRating === 'needs-improvement') {
            warnings.push(`cumulative layout shift ${layoutShift.value.toFixed(3)} over ${layoutShift.count} shifts`);
        }

        failures.push(...sameOriginFailures);

        if (traffic.pageErrors.length) {
            failures.push(...traffic.pageErrors.map((entry) => `pageerror ${entry}`));
        }
        // Allowlisted browser noise is dropped but still counted, so the
        // report says what was suppressed instead of quietly shrinking.
        warnings.push(...consoleTriage.entries.map((entry) => `console ${entry.type}: ${entry.text}`));
        if (consoleTriage.suppressed) {
            warnings.push(`console: ${consoleTriage.suppressed} allowlisted entries suppressed`);
        }
        warnings.push(...externalWarnings);

        const health = buildUIHealthReport({
            name: view.name,
            url: page.url(),
            device: view.device,
            engine: deviceProfile(view.device).engine,
            metrics,
            console: consoleTriage,
            axe,
            layoutShift,
            visualDriftRatio: visual.ratio,
        });

        if (HEALTH_MIN > 0 && health.score < HEALTH_MIN) {
            failures.push(`UI health score ${health.score} below UI_LINT_HEALTH_MIN=${HEALTH_MIN}`);
        }
        if (HEALTH_GATE && health.gates.hardBlock) {
            const blocking = health.ux.issues
                .filter((issue) => issue.severity === 'critical')
                .map((issue) => issue.kind);
            failures.push(`UI health hard block: ${[...new Set(blocking)].join(', ') || 'axe critical violation'}`);
        }

        return {
            name: view.name,
            url: page.url(),
            device: view.device,
            engine: deviceProfile(view.device).engine,
            failures,
            warnings,
            health,
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
                footerSpaceReservationMissing: metrics.footerSpaceReservationMissing,
                footerViewportPlacementBroken: metrics.footerViewportPlacementBroken,
                footerHistoryTransitionBroken: metrics.footerHistoryTransitionBroken,
                footerHistoryTransitionStates: metrics.footerHistoryTransitionStates || [],
                navbarButtonAlignment: metrics.navbarButtonAlignment || 0,
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
                jobsActionRightEdgeSpacing: metrics.jobsActionRightEdgeSpacing?.length || 0,
                escapedHtmlElements: metrics.escapedHtmlElements?.length || 0,
                importantAbuse: metrics.importantAbuse?.count || 0,
                localOverflowIssues: metrics.localOverflowIssues?.length || 0,
                brokenTitleTruncation: metrics.brokenTitleTruncation?.length || 0,
                mobileJobsFeedIssues: metrics.mobileJobsFeedIssues?.length || 0,
                tabletLayoutIssues: metrics.tabletLayoutIssues?.length || 0,
                statCardCenteringIssues: metrics.statCardCenteringIssues?.length || 0,
                mixedLayoutIssues: metrics.mixedLayoutIssues?.length || 0,
                containerWidthIssue: metrics.containerWidthIssue ? 1 : 0,
                statTileMobileLayoutIssues: metrics.statTileMobileLayoutIssues?.length || 0,
                focusIndicatorMissing: metrics.focusIndicatorMissing?.length || 0,
                ghostScrollContainers: metrics.ghostScrollContainers?.length || 0,
                nestedScrollContainers: metrics.nestedScrollContainers?.length || 0,
                flexScrollTraps: metrics.flexScrollTraps?.length || 0,
                doubleScrollRisk: metrics.doubleScrollRisk ? 1 : 0,
                viewportScrollLeak: metrics.viewportScrollLeak ? 1 : 0,
                badgeInconsistencies: metrics.badgeInconsistencies || 0,
                platformBadgeGeometryIssues: metrics.platformBadgeGeometryIssues?.length || 0,
                iconPointerEventsIssues: metrics.iconPointerEventsIssues?.length || 0,
                gridViolations: metrics.gridViolations?.length || 0,
                overlapIssues: metrics.overlapIssues?.length || 0,
                fontLoadingStatus: metrics.fontLoadingStatus?.status || 'unknown',
                externalFontRequests: metrics.externalFontRequests?.length || 0,
                materialSymbolsMissingVariationSettings: metrics.materialSymbolsMissingVariationSettings || false,
                dropdownCaretIssues: metrics.dropdownCaretIssues?.length || 0,
                brokenImages: metrics.brokenImages?.length || 0,
                brokenIcons: metrics.brokenIcons?.length || 0,
                svgIssues: metrics.svgIssues?.length || 0,
                iconFontIssues: metrics.iconFontIssues?.length || 0,
                tableOverflowIssues: metrics.tableOverflowIssues?.length || 0,
                missingMomentumScroll: metrics.missingMomentumScroll?.length || 0,
                backdropWithoutFallback: metrics.backdropWithoutFallback?.length || 0,
                invisibleMedia: metrics.invisibleMedia?.length || 0,
                backgroundAttachmentFixedWithoutFallback: metrics.backgroundAttachmentFixedWithoutFallback || false,
                loginShellAlignmentIssue: metrics.loginShellAlignmentIssue || false,
                overflowHiddenScrollBlockers: metrics.overflowHiddenScrollBlockers?.length || 0,
                flexMinHeightOverflowHidden: metrics.flexMinHeightOverflowHidden?.length || 0,
                hasSelectorLayoutUsage: metrics.hasSelectorLayoutUsage?.length || 0,
                viewportLockingIssues: metrics.viewportLockingIssues?.length || 0,
                submitCardNotShrinking: metrics.submitCardNotShrinking || false,
                jobsCardHeaderExpanding: metrics.jobsCardHeaderExpanding || false,
                tableResponsiveGhostScroll: metrics.tableResponsiveGhostScroll || false,
                dashboardViewportContractBroken: metrics.dashboardViewportContractBroken || false,
                jobsCardNotFillingHeight: metrics.jobsCardNotFillingHeight || false,
                stickyFooterDetached: metrics.stickyFooterDetached || false,
                stickyTableHeaderBroken: metrics.stickyTableHeaderBroken || false,
                jobsSentinelOutsideScrollContainer: metrics.jobsSentinelOutsideScrollContainer || false,
                jobsInfiniteScrollNotObserverBased: metrics.jobsInfiniteScrollNotObserverBased || false,
                jobsPagingOffsetContractBroken: metrics.jobsPagingOffsetContractBroken || false,
                jobsDesktopFileSizePlacementBroken: metrics.jobsDesktopFileSizePlacementBroken || false,
                jobsMobileShareActionMissing: metrics.jobsMobileShareActionMissing || false,
                trimWaveformMissingStyle: metrics.trimWaveformMissingStyle || false,
                videoPreviewContractBroken: metrics.videoPreviewContractBroken || false,
                settingsFieldStackContractBroken: metrics.settingsFieldStackContractBroken || false,
                settingsSaveToastContractBroken: metrics.settingsSaveToastContractBroken || false,
                settingsHintContractBroken: metrics.settingsHintContractBroken || false,
                settingsHintSpacingContractBroken: metrics.settingsHintSpacingContractBroken || false,
                settingsTabTitleGapInconsistent: metrics.settingsTabTitleGapInconsistent || false,
                settingsTabTitleGaps: metrics.settingsTabTitleGaps || {},
                lalalMobileActionLayoutBroken: metrics.lalalMobileActionLayoutBroken || false,
                trimDefaultSelectionInvalid: metrics.trimDefaultSelectionInvalid || false,
                uiCardChildExpands: metrics.uiCardChildExpands || 0,
                trimInvalidRanges: metrics.trimInvalidRanges || false,
                trimSnapViolations: metrics.trimSnapViolations || 0,
                trimLoopDriftMs: metrics.trimLoopDriftMs || 0,
                trimZoomInstability: metrics.trimZoomInstability || 0,
                trimHandleTooSmall: metrics.trimHandleTooSmall || 0,
                trimKeyboardMissing: metrics.trimKeyboardMissing || [],
                trimKeyboardConflicts: metrics.trimKeyboardConflicts || [],
                trimInstanceLeaks: metrics.trimInstanceLeaks || 0,
                trimUiAudioMismatchMs: metrics.trimUiAudioMismatchMs || 0,
                webkitOnlyRules: metrics.webkitOnlyRules || 0,
                colorMixWithoutFallback: metrics.colorMixWithoutFallback || 0,
                willChangeAbuse: metrics.willChangeAbuse || 0,
                zIndexAbuse: metrics.zIndexAbuse || 0,
                nestedOverflowHidden: metrics.nestedOverflowHidden || 0,
                mobileTitleCellFlexRegression: metrics.mobileTitleCellFlexRegression || false,
                mobileActionSurfaceContractBroken: metrics.mobileActionSurfaceContractBroken || false,
                emptyStateHoverHighlight: metrics.emptyStateHoverHighlight || false,
                iosInputZoomTargets: metrics.iosInputZoomTargets?.length || 0,
                viewportUnitTraps: metrics.viewportUnitTraps?.length || 0,
                safeAreaInsetsDisabled: metrics.safeAreaInsetsDisabled || false,
                bottomPinnedWithoutSafeArea: metrics.bottomPinnedWithoutSafeArea?.length || 0,
                axeAvailable: axe.available,
                axeCritical: axe.critical?.length || 0,
                axeSerious: axe.serious?.length || 0,
                axeModerate: axe.moderate?.length || 0,
                axeMinor: axe.minor?.length || 0,
                axeIncomplete: axe.incomplete || 0,
                layoutShiftSupported: Boolean(layoutShift.supported),
                layoutShiftValue: Number(Number(layoutShift.value || 0).toFixed(4)),
                layoutShiftCount: Number(layoutShift.count || 0),
                consoleSeverityScore: consoleTriage.score || 0,
                consoleSuppressed: consoleTriage.suppressed || 0,
                uiHealthScore: health.score,
            },
            // Keep actionable element-level details in the JSON report while
            // retaining the compact counters above for console output.
            details: {
                smallTouchTargets: metrics.smallTouchTargets,
                tightlyPackedTargets: metrics.tightlyPackedTargets,
                contrastIssues: metrics.contrastIssues,
                tinyText: metrics.tinyText,
                weakText: metrics.weakText,
                alignmentIssues: metrics.alignmentIssues,
                missingMomentumScroll: metrics.missingMomentumScroll,
                clippedDropdowns: metrics.clippedDropdowns,
                importantViolations: metrics.importantAbuse?.violations,
                unguardedAnimations: metrics.unguardedAnimations,
                mobileJobsFeedIssues: metrics.mobileJobsFeedIssues,
                tabletLayoutIssues: metrics.tabletLayoutIssues,
                statTileMobileLayoutIssues: metrics.statTileMobileLayoutIssues,
                platformBadgeGeometryIssues: metrics.platformBadgeGeometryIssues,
                iosInputZoomTargets: metrics.iosInputZoomTargets,
                viewportUnitTraps: metrics.viewportUnitTraps,
                bottomPinnedWithoutSafeArea: metrics.bottomPinnedWithoutSafeArea,
                axeViolations: axe.available
                    ? [...(axe.critical || []), ...(axe.serious || []), ...(axe.moderate || []), ...(axe.minor || [])]
                    : [],
                layoutShiftEntries: layoutShift.entries || [],
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

    // Engine follows the device profile: Chromium for the desktop context,
    // WebKit for every touch profile since that is what iOS and iPadOS run,
    // and Firefox for the Gecko desktop profile.
    //
    // Launch failures are recorded rather than thrown. A missing browser
    // binary is a setup problem with one fix (`npm run ui-lint:install`), and
    // failing the whole audit over it would throw away every finding the other
    // engines did produce. The views on that engine are reported as skipped
    // with the reason, so the omission is visible in the report.
    const browsers = new Map();
    const engineErrors = new Map();

    const launchEngine = async (engine) => {
        try {
            browsers.set(engine, await ENGINE_LAUNCHERS[engine].launch({ headless: true }));
        } catch (error) {
            engineErrors.set(engine, error instanceof Error ? error.message : String(error));
        }
    };

    await Promise.all(SELECTED_ENGINES.map(launchEngine));

    const browserFor = (device) => browsers.get(deviceProfile(device).engine);
    /** Reason a device profile cannot run, or null when it can. */
    const engineUnavailable = (device) => {
        const { engine } = deviceProfile(device);
        if (!SELECTED_ENGINES.includes(engine)) return `${engine} not selected by UI_LINT_BROWSERS`;
        if (engineErrors.has(engine)) return `${engine} failed to launch: ${engineErrors.get(engine)}`;
        return null;
    };

    // Auth and job discovery need one working engine but not a particular
    // one; they only produce a storage state and an id.
    const utilityBrowser = browsers.get('chromium') || browsers.values().next().value;
    if (!utilityBrowser) {
        throw new Error(
            `No browser engine could be launched (${[...engineErrors].map(([e, m]) => `${e}: ${m}`).join('; ')
            }). Run: npm run ui-lint:install`,
        );
    }

    try {
        const loginRequired = await detectLoginRequired();
        if (loginRequired && (!USERNAME || !PASSWORD)) {
            throw new Error(
                'UI_LINT_USERNAME and UI_LINT_PASSWORD are required when login is enabled',
            );
        }
        const authState = loginRequired ? await createAuthState(utilityBrowser) : {};
        const firstJobId = process.env.UI_LINT_JOB_ID
            || await discoverJobId(utilityBrowser, authState);
        const results = [];

        for (const device of ['desktop', 'mobile', 'tablet']) {
            const unavailable = engineUnavailable(device);
            if (unavailable) {
                results.push(buildSkippedResult(
                    invalidLoginResultName(device),
                    `${BASE_URL}/login`,
                    `skipped invalid-login check: ${unavailable}`,
                ));
                continue;
            }
            results.push(await runInvalidLoginCheck(browserFor(device), loginRequired, device));
        }

        // One queue per engine rather than one shared queue: the engines are
        // separate processes and overlap freely, while the pages inside one
        // engine share it and need their own limit. A single pool of two
        // workers serialised Chromium behind WebKit for no reason.
        const parallelResults = new Array(VIEW_DEFS.length);
        const byEngine = new Map();
        VIEW_DEFS.forEach((view, index) => {
            const { engine } = deviceProfile(view.device);
            if (!byEngine.has(engine)) byEngine.set(engine, []);
            byEngine.get(engine).push({ view, index });
        });

        const runEngineQueue = async (engine, queue) => {
            const unavailable = engineUnavailable(queue[0].view.device);
            if (unavailable) {
                for (const { view, index } of queue) {
                    parallelResults[index] = buildSkippedResult(
                        view.name,
                        `${BASE_URL}${view.url}`,
                        `skipped: ${unavailable}`,
                    );
                }
                return;
            }

            const pending = [...queue];
            const workers = Array.from({ length: DEVICE_CONCURRENCY }, async () => {
                while (pending.length) {
                    const item = pending.shift();
                    if (!item) break;
                    if (LOGIN_VIEW_NAMES.includes(item.view.name) && !loginRequired) {
                        parallelResults[item.index] = buildSkippedResult(
                            item.view.name,
                            `${BASE_URL}${item.view.url}`,
                            'login is disabled; skipped login page audit',
                        );
                        continue;
                    }
                    parallelResults[item.index] = await runViewSafely(
                        browsers.get(engine),
                        authState,
                        item.view,
                    );
                }
            });
            await Promise.all(workers);
        };

        const engineQueue = [...byEngine.entries()];
        const engineWorkers = Array.from({ length: BROWSER_CONCURRENCY }, async () => {
            while (engineQueue.length) {
                const entry = engineQueue.shift();
                if (!entry) break;
                await runEngineQueue(entry[0], entry[1]);
            }
        });
        await Promise.all(engineWorkers);
        results.push(...parallelResults.filter(Boolean));

        if (firstJobId) {
            const jobDetailView = {
                url: '/job/:jobId',
                readySelector: '#status',
                auth: true,
                requiredSelectors: ['#status', '#message'],
            };
            for (const [name, device] of [
                ['job-detail', 'desktop'],
                ['mobile-job-detail', 'mobile'],
                ['tablet-job-detail', 'tablet'],
            ]) {
                const unavailable = engineUnavailable(device);
                if (unavailable) {
                    results.push(buildSkippedResult(
                        name,
                        `${BASE_URL}/job/${firstJobId}`,
                        `skipped: ${unavailable}`,
                    ));
                    continue;
                }
                results.push(await runViewSafely(browserFor(device), authState, {
                    ...jobDetailView,
                    name,
                    device,
                }, { jobId: firstJobId }));
            }
        }

        const totals = results.reduce((acc, result) => {
            acc.failures += result.failures.length;
            acc.warnings += result.warnings.length;
            return acc;
        }, { failures: 0, warnings: 0 });

        const health = summarizeHealthReports(results.map((result) => result.health).filter(Boolean));

        const payload = {
            baseUrl: BASE_URL,
            outputDir: OUTPUT_DIR,
            generatedAt: new Date().toISOString(),
            engines: {
                selected: SELECTED_ENGINES,
                launched: [...browsers.keys()],
                failed: Object.fromEntries(engineErrors),
            },
            results,
            totals,
            health,
        };

        await writeFile(RESULTS_PATH, JSON.stringify(payload, null, 2));

        console.log('UI_LINT_START');
        console.log(`Output: ${OUTPUT_DIR}`);
        for (const result of results) {
            // A skipped view is not a passing one. Before Firefox could be
            // missing this only ever meant "login is disabled", but an engine
            // that failed to launch printing PASS reads as coverage that never
            // happened.
            const status = result.failures.length ? 'FAIL' : result.metrics?.skipped ? 'SKIP' : 'PASS';
            console.log(`${status} ${result.name} ${formatResultSummary(result)}`.trim());
            for (const failure of result.failures) {
                console.log(`  hard: ${failure}`);
            }
            for (const warning of result.warnings) {
                console.log(`  warn: ${warning}`);
            }
        }
        console.log(`Totals: failures=${totals.failures} warnings=${totals.warnings}`);
        if (health.views) {
            console.log(
                `UI health: worst=${health.worstScore} average=${health.averageScore} `
                + `(healthy=${health.healthy} degraded=${health.degraded} critical=${health.critical})`,
            );
            if (health.hardBlocked.length) {
                console.log(`Hard-blocked views: ${health.hardBlocked.join(', ')}`);
            }
        }
        for (const [engine, message] of engineErrors) {
            console.log(`Engine unavailable: ${engine} (${message})`);
        }
        console.log(`Results JSON: ${RESULTS_PATH}`);

        process.exitCode = totals.failures > 0 ? 1 : 0;
    } finally {
        // Isolate cleanup errors so they never shadow the original exception.
        for (const [engine, instance] of browsers) {
            try {
                await instance.close();
            } catch (closeErr) {
                console.error(`${engine} cleanup failed:`, closeErr);
            }
        }
    }
}

// Exported so tests/js can assert the device model without launching browsers.
export { DEVICE_PROFILES, VIEW_DEFS, COMPACT_LAYOUT_MAX_WIDTH, createContextOptions, deviceProfile, profileHasTouch, profileIsCompactLayout };

// Only audit when run as a program. Importing the module (from a test, or to
// reuse the registry) must not start Playwright.
const invokedDirectly = process.argv[1]
    && import.meta.url === pathToFileURL(process.argv[1]).href;

if (invokedDirectly) {
    main().catch((error) => {
        console.error(error instanceof Error ? error.stack || error.message : String(error));
        process.exit(1);
    });
}
