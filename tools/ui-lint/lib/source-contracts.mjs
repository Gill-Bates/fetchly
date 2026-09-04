//
// tools/ui-lint/lib/source-contracts.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Source-level contract checks for the jobs list and the settings page.
//
// These rules are the odd ones out in the audit: they never touch a page.
// They read app/static/js/*.js, app/templates/settings.html and
// app/static/style.css and assert that a handful of invariants are still
// spelled out in the source - the paging offset stays monotonic, infinite
// scroll stays bound to the local scroller, the mobile job row keeps the
// shared Download/Share menu, settings hints stay the icon component.
//
// They lived inside run-ui-lint.mjs, which meant they only ran behind a
// Playwright launch, a logged-in session and a seeded database. That is a lot
// of moving parts for a regex, and it kept them out of CI entirely. Extracted
// here they have no dependency beyond node:fs, so the same checks run as a
// plain lint step (see ../check-source-contracts.mjs) and still feed the
// per-view metrics of the full audit.
//
// Nothing in this file may import playwright, or the CI lint step needs the
// ui-lint sub-project installed again.
//
import { readFile } from 'node:fs/promises';

const MAIN_JS_PATH = new URL('../../../app/static/js/main.js', import.meta.url);
const JOBS_JS_PATH = new URL('../../../app/static/js/jobs.js', import.meta.url);
const UI_JS_PATH = new URL('../../../app/static/js/ui.js', import.meta.url);
const SETTINGS_JS_PATH = new URL('../../../app/static/js/settings.js', import.meta.url);
const SETTINGS_TEMPLATE_PATH = new URL('../../../app/templates/settings.html', import.meta.url);
const SETTINGS_STYLE_PATH = new URL('../../../app/static/style.css', import.meta.url);

/** Metric keys produced by the jobs-list contracts, in report order. */
export const JOBS_SOURCE_CONTRACT_KEYS = Object.freeze([
    'jobsInfiniteScrollNotObserverBased',
    'jobsPagingOffsetContractBroken',
    'jobsDesktopFileSizePlacementBroken',
    'jobsMobileShareActionMissing',
]);

/** Metric keys produced by the settings contracts, in report order. */
export const SETTINGS_SOURCE_CONTRACT_KEYS = Object.freeze([
    'settingsSaveToastContractBroken',
    'settingsHintContractBroken',
    'settingsHintSpacingContractBroken',
]);

export const SOURCE_CONTRACT_KEYS = Object.freeze([
    ...JOBS_SOURCE_CONTRACT_KEYS,
    ...SETTINGS_SOURCE_CONTRACT_KEYS,
]);

/**
 * The single wording for every contract, shared by the browser runner's
 * per-view failure list and the standalone lint step. Two copies would drift
 * and a reader would not know which one the CI log came from.
 */
export const SOURCE_CONTRACT_MESSAGES = Object.freeze({
    jobsInfiniteScrollNotObserverBased: 'jobs infinite scroll is not driven by an IntersectionObserver rooted at the local scroller',
    jobsPagingOffsetContractBroken: 'jobs pagination offset is not monotonic across row trimming',
    jobsDesktopFileSizePlacementBroken: 'desktop job file size must share the Media metadata line and stay out of Status',
    jobsMobileShareActionMissing: 'mobile downloadable jobs must expose the shared Download and Share menu',
    settingsSaveToastContractBroken: 'settings autosave must stay quiet, while errors use toasts and no inline save-status row is rendered',
    settingsHintContractBroken: 'settings explanation hints must use the info-icon hint style',
    settingsHintSpacingContractBroken: 'settings explanation hints must share the global 4px spacing rule',
});

/**
 * Repo-relative files each contract reads, so a failure can point at
 * something. The regexes span whole files, so there is no line to give.
 */
export const SOURCE_CONTRACT_FILES = Object.freeze({
    jobsInfiniteScrollNotObserverBased: Object.freeze(['app/static/js/main.js']),
    jobsPagingOffsetContractBroken: Object.freeze(['app/static/js/jobs.js']),
    jobsDesktopFileSizePlacementBroken: Object.freeze(['app/static/js/jobs.js']),
    jobsMobileShareActionMissing: Object.freeze(['app/static/js/ui.js']),
    settingsSaveToastContractBroken: Object.freeze(['app/static/js/settings.js', 'app/templates/settings.html']),
    settingsHintContractBroken: Object.freeze(['app/templates/settings.html', 'app/static/style.css']),
    settingsHintSpacingContractBroken: Object.freeze(['app/static/style.css']),
});

/**
 * Every contract reported as satisfied. Views that do not render the jobs list
 * or the settings form spread these in so the metric shape stays uniform.
 * @param {readonly string[]} keys
 * @returns {Readonly<Record<string, false>>}
 */
function passing(keys) {
    return Object.freeze(Object.fromEntries(keys.map((key) => [key, false])));
}

export const JOBS_SOURCE_CONTRACT_DEFAULTS = passing(JOBS_SOURCE_CONTRACT_KEYS);
export const SETTINGS_SOURCE_CONTRACT_DEFAULTS = passing(SETTINGS_SOURCE_CONTRACT_KEYS);

let jobsSourceContractMetricsPromise;
let settingsSourceContractMetricsPromise;

export async function getJobsSourceContractMetrics() {
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

export async function getSettingsSourceContractMetrics() {
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
            // The contract is the *component*: every hint is the icon variant
            // and leads with a setting-hint-icon span. The glyph is not part
            // of it - a hint that explains a forced setting says `lock` and
            // one that warns about a cost says `warning`, and demanding
            // `info` everywhere would make those hints worse to satisfy the
            // lint rather than the other way round. The allowlist keeps the
            // set deliberate without pinning it to one icon.
            const HINT_ICONS = ['info', 'lock', 'warning'];
            const hintIconPattern = new RegExp(
                `^\\s*<span\\b[^>]*\\bclass=["'][^"']*\\bmaterial-symbols-outlined\\b[^"']*\\bsetting-hint-icon\\b[^"']*["'][^>]*>\\s*(?:${HINT_ICONS.join('|')})\\s*<\\/span>`,
            );
            const hintContractBroken = hints.length === 0 || hints.some((hint) => {
                const openingTag = hint[0].slice(0, hint[0].indexOf('>') + 1);
                const content = hint[2];
                return !openingTag.includes('setting-hint--with-icon')
                    || !hintIconPattern.test(content);
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

/**
 * Every source contract at once, for callers with no per-view scoping.
 * @returns {Promise<Record<string, boolean>>}
 */
export async function collectSourceContractMetrics() {
    const [jobs, settings] = await Promise.all([
        getJobsSourceContractMetrics(),
        getSettingsSourceContractMetrics(),
    ]);

    return { ...jobs, ...settings };
}

/**
 * Turns a metric bag into the violated contracts, in report order.
 * @param {Record<string, boolean>} metrics
 * @param {readonly string[]} [keys] subset to consider
 * @returns {{ key: string, message: string, files: readonly string[] }[]}
 */
export function sourceContractViolations(metrics, keys = SOURCE_CONTRACT_KEYS) {
    return keys
        .filter((key) => metrics[key])
        .map((key) => ({
            key,
            message: SOURCE_CONTRACT_MESSAGES[key],
            files: SOURCE_CONTRACT_FILES[key] || [],
        }));
}
