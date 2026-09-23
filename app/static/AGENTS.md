<!-- Parent: ../AGENTS.md -->

# app/static/ — Web Assets (CSS, JavaScript, Images)

**Purpose:** Static assets served directly to browsers: hand-written ES modules, stylesheets, images, self-hosted fonts, and vendored third-party libraries (Bootstrap 5, WaveSurfer.js).

Static files here are served by `VersionedStaticFiles` (`app/utils/assets.py`) without server-side processing — the subclass only sets `Cache-Control`, it does not touch the bytes. There is **no build step, no bundler and no frontend framework** — the `.js` files in `js/` are the files the browser loads, and they use native ES module `import`/`export`.

## Key Files

| File | Purpose |
| --- | --- |
| `js/main.js` | Dashboard entry point: page initialization, DOM setup, event listener registration |
| `js/api.js` | API client: typed wrappers (`fetchJobs`, `fetchJob`, `submitJob`, `fetchStats`, `fetchVideoInfo`, …), CSRF header injection, error normalization via `toErrorMessage()` |
| `js/config.js` | Frontend contract constants: `CONFIG`, `AUDIO_TYPE`, Lalal duration caps, and the status sets `DOWNLOADABLE_STATUSES`, `CANCELLABLE_STATUSES`, `RETRYABLE_STATUSES`, `TERMINAL_STATUSES` |
| `js/ui.js` | Shared UI vocabulary: `STATUS_META`, `ACTION_CATEGORY`, `normalizeStatus()`, `getStatusText()`, `getStatusPillClass()`, `toProgressPercent()` |
| `js/utils.js` | Generic helpers: `clamp()`, `snapTime()`, `normalizeTimeRange()`, `buildTrimId()`, `createTimeoutSignal()`, `combineAbortSignals()`, `EMPTY_VALUE` |
| `js/jobs.js` | Job list UI: rendering, pagination, filtering, live status updates |
| `js/job.js` | Job detail view: single job display, download controls, action buttons |
| `js/trim.js` | Audio trimming editor: WaveSurfer.js waveform, region markers, in/out points |
| `js/settings.js` | Settings page: form handling, watermark upload, cookie management, system stats |
| `js/login.js` | Login form handling and error display |
| `js/events.js` | **SSE client**: opens `new EventSource("/events")`, routes messages, handles reconnect |
| `js/toast.js` | Toast notifications: success/error/info, auto-dismiss, manual dismiss |
| `js/confirm.js` | Confirmation modal (`confirmModal()`). Use this for destructive actions — never `window.confirm`/`alert`/`prompt`. |
| `js/current-job.js` | Active job state: `setCurrentJob()`, `refreshCurrentJob()` |
| `js/cookie-paste.js` | Cookie import UI: paste field, format validation, platform detection |
| `js/watermark-logo.js` | Client-side logo validation: `prepareLogoUpload()`, `inspectSvg()`, `inspectPng()`, `rasterizeSvg()`, `MAX_LOGO_BYTES` (2 MiB) |
| `js/errors.js` | `reportError()` / `reportWarning()` with context |
| `js/boot-flags.js` | Side-effect script, no exports: sets boot-time flags on the document |
| `js/navbar-logo.js` | Side-effect script, no exports: navbar branding |
| `js/share-error.js` | Side-effect script, no exports: share-link error page behavior |
| `style.css` | Main stylesheet (at the `static/` root, not in a `css/` directory) |
| `login.css` | Login page stylesheet |

## Subdirectories

| Directory | Purpose |
| --- | --- |
| `js/` | ES modules, served as-is |
| `img/` | `favicon.ico`, `favicon.svg`, `fetchly_logo.svg`, `fetchly_watermark.png`, Lalal.ai logos, `social/` |
| `fonts/` | Self-hosted webfonts: `material-symbols-outlined.woff2`, `roboto-flex.woff2`, `roboto-flex.ttf` |
| `vendor/` | `bootstrap.bundle.min.js`, `bootstrap.min.css`, `wavesurfer/dist/wavesurfer.esm.js`, `wavesurfer/dist/plugins/regions.esm.js` |

There is no `css/` subdirectory. Stylelint globs `app/static/**/*.css`, which picks up both root-level stylesheets.

## For AI Agents

### Working on JavaScript
- **Module structure:** native ES modules — `export function foo()` and `import { foo } from "./foo.js"`. Relative paths with the `.js` extension; no bare specifiers, no bundler resolution, and no `?v=` cache-busting token (ESLint rejects one — `/static` is served with content-hashed URLs, see `app/utils/assets.py`).
- **DOM selectors:** prefer `data-*` attributes for stable hooks
- **Event handling:** `addEventListener()`; no inline `onclick`
- **Async:** use the `api.js` wrappers rather than raw `fetch()` — they attach the CSRF header and normalize errors
- **Dialogs:** `confirmModal()` from `confirm.js`. Native browser dialogs are not used anywhere in this codebase.
- **Error display:** surface user-facing failures through `toast.js`
- **Live updates:** subscribe via `events.js` (SSE)
- **Testing:** `tests/js/*.test.mjs`, run with `npm test`

### The status contract
`config.js` mirrors the job status vocabulary from `app/db.py`. The two must agree: `tools/ui-lint/check-source-contracts.mjs` (run by `npm run lint:contracts`) and `tests/js/config-contract.test.mjs` both enforce it. If you add or rename a status in `db.py`, update `config.js` and `ui.js` in the same change or CI fails.

### Working on Styles
- **Stylesheets:** `style.css` and `login.css` at the `static/` root
- **Bootstrap 5** is vendored and loaded from `vendor/`. Prefer its utilities over new custom CSS where they fit.
- **Theme support:** the app is dark-only (`color-scheme: dark`, no light overrides); follow the existing custom-property pattern in `style.css`
- **Fonts:** self-hosted from `fonts/`. Do not add Google Fonts or CDN links — the CSP forbids them and `tests/test_csp_wavesurfer.py` guards related policy.
- **Accessibility:** sufficient contrast, semantic HTML, ARIA labels where icons carry meaning

### Adding a New UI Component
1. Create `js/new-component.js` and export an init function
2. Add styles to `style.css`
3. Import and initialize it from `js/main.js` (or the relevant page entry point)
4. Add `tests/js/new-component.test.mjs` using `node:test` and `helpers/fake-dom.mjs`
5. Run `npm test` and `npm run lint`

### Working with WaveSurfer.js
- Imported from `vendor/wavesurfer/dist/wavesurfer.esm.js`, with the regions plugin from `dist/plugins/regions.esm.js`
- Used by `trim.js` for waveform rendering and trim-region markers
- The CSP interaction is covered by `tests/test_csp_wavesurfer.py`; check it before changing how the bundle is loaded

## Dependencies

### Internal
- `../templates/` — the HTML that loads these files
- `../routes/` — the endpoints `api.js` calls
- `../db.py` — the source of truth for the status vocabulary mirrored in `config.js`

### External
- **Bootstrap 5:** vendored CSS + JS bundle
- **WaveSurfer.js:** vendored ESM build and regions plugin
- **Browser APIs:** Fetch, EventSource, localStorage, AbortSignal

## Manual

### Building Static Assets
No build step exists. Files in `js/` and the root-level CSS are served exactly as committed. There is no `npm run build` script.

### Testing JavaScript
```bash
npm test                    # node --test over tests/js/*.test.mjs
npm run lint:js             # ESLint
npm run lint:css            # Stylelint over app/static/**/*.css
npm run lint:contracts      # source-contract check, incl. the status vocabulary
```

### Adding a Vendored Library
1. Download the built ESM bundle
2. Place it under `vendor/<name>/`
3. Import it with a relative path from the module that needs it
4. Check the CSP still allows it

### Debugging JavaScript
Open DevTools: Console for errors, Network to confirm API calls and the `/events` stream, Application for stored flags.

---

**Last Updated:** 2026-09-19  
**JavaScript:** Native ES modules, no build step, no framework  
**CSS:** Vanilla CSS plus vendored Bootstrap 5  
**Audio Library:** WaveSurfer.js (vendored)  
**Live Updates:** Server-Sent Events
