//
// tools/ui-lint/run-ui-lint.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { chromium, devices, webkit } from 'playwright';

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
const OUTPUT_DIR = process.env.UI_LINT_OUTPUT_DIR || `/tmp/fetchly-ui-lint-${SESSION_ID}`;
const SCREENSHOT_DIR = path.join(OUTPUT_DIR, 'screenshots');
const RESULTS_PATH = path.join(OUTPUT_DIR, 'results.json');
const MAIN_JS_PATH = new URL('../../app/static/js/main.js', import.meta.url);
const JOBS_JS_PATH = new URL('../../app/static/js/jobs.js', import.meta.url);
const UI_JS_PATH = new URL('../../app/static/js/ui.js', import.meta.url);
const SETTINGS_JS_PATH = new URL('../../app/static/js/settings.js', import.meta.url);
const SETTINGS_TEMPLATE_PATH = new URL('../../app/templates/settings.html', import.meta.url);

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
            const [settingsJsSource, settingsTemplateSource] = await Promise.all([
                readFile(SETTINGS_JS_PATH, 'utf8'),
                readFile(SETTINGS_TEMPLATE_PATH, 'utf8'),
            ]);

            const successUsesToast = /showToast\(\s*payload\.message\s*\|\|\s*["']Settings updated["']\s*,\s*["']success["']\s*\)/.test(settingsJsSource);
            const validationUsesToast = /showToast\(\s*validation\.error\s*,\s*["']danger["']\s*\)/.test(settingsJsSource);
            const jsUsesInlineSaveStatus = /\b(?:AUTO_SAVE_STATE|setAutoSaveState|autoSaveIndicator|settingsSaveBtn|settingsAlert)\b/.test(settingsJsSource);
            const templateHasInlineSaveStatus = /(?:id=["'](?:autoSaveIndicator|settingsSaveBtn|settingsAlert)["']|class=["'][^"']*settings-status-bar)/.test(settingsTemplateSource);

            return {
                settingsSaveToastContractBroken: !successUsesToast
                    || !validationUsesToast
                    || jsUsesInlineSaveStatus
                    || templateHasInlineSaveStatus,
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
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsRenderRoot', '#jobsTable', '#wsIndicator'],
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
        name: 'mobile-dashboard',
        url: '/',
        readySelector: '#submitForm',
        auth: true,
        device: 'mobile',
        requiredSelectors: ['.stats-row', '#submitForm', '#jobsRenderRoot', '#jobsMobileList', '#showJobHistoryToggle', '#wsIndicator'],
    },
    {
        name: 'mobile-settings',
        url: '/settings',
        readySelector: '#settingsForm',
        auth: true,
        device: 'mobile',
        requiredSelectors: ['#settingsForm'],
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
            // The application CSP correctly blocks inline styles. The lint
            // runner injects its own stability stylesheet, so bypass CSP only
            // in this isolated test context.
            bypassCSP: true,
        };
    }

    return {
        viewport: { width: 1440, height: 1200 },
        screen: { width: 1440, height: 1200 },
        deviceScaleFactor: 1,
        hasTouch: false,
        colorScheme: 'dark',
        bypassCSP: true,
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
    if (metrics.lalalMobileActionLayoutBroken) parts.push('lalalMobileActionLayoutBroken=true');
    if (metrics.trimDefaultRegionPresent) parts.push('trimDefaultRegion=true');
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
    lalalMobileActionLayoutBroken: false,
    trimDefaultRegionPresent: false,
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
        stableFrames: view.device === 'mobile' ? MOBILE_LAYOUT_STABLE_FRAMES : DESKTOP_LAYOUT_STABLE_FRAMES,
        maxFrames: view.device === 'mobile' ? MOBILE_LAYOUT_MAX_FRAMES : DESKTOP_LAYOUT_MAX_FRAMES,
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

        const touchTargetMin = isMobile ? mobileTouchTargetMin : desktopTouchTargetMin;
        const smallTouchTargets = Array.from(document.querySelectorAll(interactiveSelector))
            .filter((el) => isVisible(el) && !el.disabled)
            .filter((el) => !isMobile || el.matches('button, .btn, [role="button"], input, select, textarea'))
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

        // ─────────────────────────────────────────────────────────────
        // iOS Scroll Blocking / Layout Bug Detection
        // ─────────────────────────────────────────────────────────────

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

        const containerWidthIssue = isMobile
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
        const statTileMobileLayoutIssues = isMobile
            ? (() => {
                const issues = [];
                const cards = Array.from(document.querySelectorAll('.stats-row .stat-card'))
                    .filter((card) => isLayoutVisible(card));

                if (cards.length > 1) {
                    const tops = new Set(cards.map((card) => Math.round(card.getBoundingClientRect().top)));
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

        // ─── NEW CSS REVIEW CHECKS ────────────────────────────────────────────────

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

        // 13. iOS background-attachment:fixed without @supports fallback
        //     Detects fixed attachment on .app-root / body without a matching scroll override.

        // ─── iOS / Rendering Stability Checks ────────────────────────────────────

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
        const missingMomentumScroll = isMobile && supportsMomentumScroll
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

        // ─── LAYOUT STABILITY CHECKS (2026-04-28) ─────────────────────────────────

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

        // Lalal actions share one equal two-column row on mobile. If the
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

        // 25. Trim modal should not have default region on open (user must select)
        const trimDefaultRegionPresent = (() => {
            const trimModal = document.querySelector('#trimModal');
            if (!trimModal || !trimModal.classList.contains('show')) return false;
            // Check if a region exists immediately after modal open
            const regions = document.querySelectorAll('#trimWave [data-id], #trimWave .wavesurfer-region');
            const infoText = document.querySelector('#trimInfo');
            // If info shows a time range (not "Click and drag" prompt), region was auto-created
            if (infoText && infoText.textContent) {
                const text = infoText.textContent.trim();
                // Should show instruction, not a pre-selected range
                const hasTimeRange = /\d+:\d+.*–.*\d+:\d+/.test(text);
                const hasInstruction = text.toLowerCase().includes('click') || text.toLowerCase().includes('drag');
                if (hasTimeRange && !hasInstruction && regions.length > 0) {
                    return true;
                }
            }
            return false;
        })();

        // 26. UI card children should not expand unless explicitly needed
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
        let trimKeyboardMissing = [];
        let trimKeyboardConflicts = [];
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
            trimDefaultRegionPresent,
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
        };
    }, {
        requiredSelectors: view.requiredSelectors,
        mobileTouchTargetMin: MOBILE_TOUCH_TARGET_MIN,
        desktopTouchTargetMin: DESKTOP_TOUCH_TARGET_MIN,
        isMobile: view.device === 'mobile',
        auditPlatformBadges: ['dashboard', 'mobile-dashboard', 'job-detail'].includes(view.name),
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
        }).catch(() => {});
    }
    if (view.device === 'mobile') {
        const style = await page.addStyleTag({ content: MOBILE_VISUAL_STABILITY_CSS });
        await style.evaluate((element) => {
            element.dataset.uiLintInjected = 'true';
        }).catch(() => {});
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
    if (!['dashboard', 'mobile-dashboard'].includes(view.name)) {
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
 * Opens the first real trim trigger and exercises keyboard behavior with
 * Playwright input. The dashboard may legitimately have no audio jobs, in
 * which case the dynamic audit is reported as skipped.
 * @param {import('playwright').Page} page
 * @param {object} view
 * @returns {Promise<{failures: string[], warnings: string[], metrics: object}>}
 */
async function auditTrimModal(page, view) {
    if (view.name !== 'dashboard') {
        return { failures: [], warnings: [], metrics: {} };
    }

    const trigger = page.locator('[data-action="open-trim"]').first();
    try {
        await trigger.waitFor({ state: 'attached', timeout: 3000 });
    } catch {
        return {
            failures: [],
            warnings: ['trim audit skipped: no audio trim trigger is rendered'],
            metrics: {},
        };
    }

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

        const trimDefaultRegionPresent = await page.evaluate(() => (
            document.querySelectorAll('#trimWave [data-id], #trimWave .wavesurfer-region').length > 0
            && !/click|drag/i.test(document.querySelector('#trimInfo')?.textContent || '')
        ));

        const trimKeyboardConflicts = [];
        if (!keyState.events.some(({ key }) => key === ' ')) {
            trimKeyboardMissing.push(' ');
        }
        if (!keyState.spacePrevented || afterScroll !== beforeScroll) {
            trimKeyboardConflicts.push('space-scroll');
        }

        await page.locator('#trimModal .btn-close').click().catch(() => {});
        await page.locator('#trimModal.show').waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
        return {
            failures: [],
            warnings: [],
            metrics: {
                trimDefaultRegionPresent,
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
            }).catch(() => {});
        }
        await page.locator('#trimModal .btn-close').click().catch(() => {});
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
            warnings.push(...traffic.consoleEntries
                .filter((entry) => !/\b401\b|unauthorized/i.test(entry.text))
                .map((entry) => `console ${entry.type}: ${entry.text}`));
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

        const runtimeMetrics = await collectMetrics(page, view);
        const footerHistoryAudit = await auditFooterHistoryToggle(page, view);
        const trimAudit = await auditTrimModal(page, view);
        const emptyStateHoverAudit = await auditEmptyStateHover(page, view);
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
            : { settingsSaveToastContractBroken: false };
        const metrics = {
            ...runtimeMetrics,
            ...footerHistoryAudit,
            ...trimAudit.metrics,
            ...emptyStateHoverAudit,
            ...jobsSourceContracts,
            ...settingsSourceContracts,
        };
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
            failures.push('settings changes must use toasts and must not render an inline save-status row');
        }
        if (metrics.lalalMobileActionLayoutBroken) {
            failures.push('Lalal mobile actions must use one equal two-column row');
        }
        if (metrics.trimDefaultRegionPresent) {
            failures.push('trim modal opens with a preselected region');
        }
        if (metrics.uiCardChildExpands) {
            failures.push(`ui-card children expanding unexpectedly: ${metrics.uiCardChildExpands}`);
        }
        if (metrics.emptyStateHoverHighlight) {
            failures.push('empty jobs table row highlights on hover like an interactive row');
        }
        applyMetricRules(metrics, failures, warnings);

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
                lalalMobileActionLayoutBroken: metrics.lalalMobileActionLayoutBroken || false,
                trimDefaultRegionPresent: metrics.trimDefaultRegionPresent || false,
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
                statTileMobileLayoutIssues: metrics.statTileMobileLayoutIssues,
                platformBadgeGeometryIssues: metrics.platformBadgeGeometryIssues,
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
    let mobileBrowser;

    try {
        const loginRequired = await detectLoginRequired();
        if (loginRequired && (!USERNAME || !PASSWORD)) {
            throw new Error(
                'UI_LINT_USERNAME and UI_LINT_PASSWORD are required when login is enabled',
            );
        }
        mobileBrowser = await webkit.launch({ headless: true });
        const authState = loginRequired ? await createAuthState(browser) : {};
        const firstJobId = loginRequired
            ? process.env.UI_LINT_JOB_ID || await discoverJobId(browser, authState)
            : null;
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
                const browserForView = item.view.device === 'mobile' ? mobileBrowser : browser;
                parallelResults[item.index] = await runViewSafely(browserForView, authState, item.view);
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
        try {
            await mobileBrowser?.close();
        } catch (closeErr) {
            console.error('WebKit cleanup failed:', closeErr);
        }
    }
}

main().catch((error) => {
    console.error(error instanceof Error ? error.stack || error.message : String(error));
    process.exit(1);
});
