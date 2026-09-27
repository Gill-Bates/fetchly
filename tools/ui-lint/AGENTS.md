<!-- Parent: ../AGENTS.md -->

# tools/ui-lint/ — UI Audit (Playwright)

**Purpose:** Automated UI quality audit against a running fetchly instance: accessibility (axe-core), layout stability, console severity, health metrics, and device-profile rendering across three browser engines.

This is a **separate tool with its own npm package and its own browsers**, not part of `npm test`. It drives real pages with Playwright, so it needs a running server.

## Key Files

| File | Purpose |
| --- | --- |
| `run-ui-lint.mjs` | The audit runner: holds `DEVICE_PROFILES`, logs in, walks the pages, applies every check, writes screenshots, aggregates results and sets the exit code |
| `check-source-contracts.mjs` | Static source check (no browser): verifies the frontend mirrors the backend's shared vocabulary and that templates version `/static` URLs through `asset_url()`. Run separately by `npm run lint:contracts`. |
| `lib/axe.mjs` | axe-core integration: tag selection (`AXE_TAGS`) and violation handling |
| `lib/layout-shift.mjs` | Layout-shift measurement |
| `lib/console-severity.mjs` | Console capture and severity classification |
| `lib/browser-utils.mjs` | Browser and page helpers |
| `lib/ui-health.mjs` | UI health metrics |
| `lib/source-contracts.mjs` | The rules `check-source-contracts.mjs` applies |
| `package.json` | Dependencies: `playwright`, `@axe-core/playwright`, `pixelmatch`, `pngjs`. Scripts: `audit`, `install:browsers`. **No `test` script.** |

## For AI Agents

### Running the Audit
From the repo root:

```bash
npm run ui-lint:install     # once: npm --prefix tools/ui-lint install && npm --prefix tools/ui-lint run install:browsers
                            # (CI uses `npm ci --prefix tools/ui-lint` instead — see ui-audit.yml)
npm run ui-lint             # npm --prefix tools/ui-lint run audit → node run-ui-lint.mjs
```

The runner targets `UI_LINT_BASE_URL`, defaulting to `http://127.0.0.1:8000`, so start the app first (`python run.py`). Screenshots are written under the runner's output directory.

`npm test` does **not** run this. That command is `node --test "tests/js/*.test.mjs"` and launches no browser.

### Device Profiles
`DEVICE_PROFILES` in `run-ui-lint.mjs`. Six entries:

| Profile | Engine | Device / viewport | Why it exists |
| --- | --- | --- | --- |
| `desktop` | chromium | 1440x1200 | The baseline desktop layout |
| `mobile` | webkit | iPhone 13 | Phone layout and touch targets |
| `tablet` | webkit | iPad Mini (768x1024) | Inside the compact band, so the jobs feed renders |
| `tablet-landscape` | webkit | iPad Mini landscape (1024x768) | The exact `max-width: 1024px` boundary — off-by-one breakpoint errors surface only here |
| `tablet-wide` | webkit | iPad Pro 11 landscape (1194x834) | Past the 1024px breakpoint but inside `(max-width: 1366px) and (pointer: coarse)`; the widest tablet, covering wide-band rules the narrower iPads never reach |
| `desktop-firefox` | firefox | 1440x1200 | Varies the **engine**, not the layout — Gecko resolves flexbox min-size, scrollbar gutters and subgrid differently, and that only shows with everything else held constant |

**Form factor and touch are deliberately separate axes.** `formFactor` says what kind of device it is; touch capability is independent of it. A narrow non-touch window is not a phone, and a touch iPad must not be given the 32px desktop touch-target minimum. Preserve both axes when adding a profile.

### Understanding Accessibility Violations
- axe-core runs through `@axe-core/playwright`; the tag set is `AXE_TAGS` in `lib/axe.mjs`
- Results are categorized by impact (critical, serious, moderate, minor)
- Typical causes: missing accessible names on icon buttons, insufficient contrast, invalid ARIA
- Fix the markup in `app/templates/` or the module in `app/static/js/`, then re-run

### Layout Stability
The runner captures two full-page screenshots after the layout has settled, with animations disabled, and compares them with `pixelmatch`. A difference means the page is still moving when it should be static. Common causes: media without intrinsic dimensions, late-loading content with no reserved space, fonts swapping in.

Media is deliberately left visible during screenshots — hiding it would change the DOM under test.

### Console Severity
`lib/console-severity.mjs` classifies captured console output. An error from the page fails the audit; the runner also treats a small set of known-benign conditions as warnings (for example undersized touch targets), which is why warning and failure are distinct levels rather than one threshold.

### Adding a New Check
1. Put the pure logic in a `lib/*.mjs` module and export it
2. Call it from `run-ui-lint.mjs` where the page is already loaded
3. Unit-test the pure part from `tests/js/ui-lint-<name>.test.mjs` with `node:test` — that suite imports these modules dynamically (`await import("../../tools/ui-lint/lib/…")`) and runs without a browser
4. Document it here

## Dependencies

### Internal
- `/app/templates/`, `/app/static/` — what the audit renders and inspects
- `/tests/js/ui-lint-*.test.mjs` — unit tests for this library's pure logic (they depend on this directory, not the other way around)

### External
- **Playwright:** chromium, webkit and firefox, installed under this prefix
- **@axe-core/playwright:** accessibility auditing
- **pixelmatch / pngjs:** screenshot comparison

## Manual

### Setting Up Locally
```bash
npm run ui-lint:install
python run.py &                  # the audit needs a live server
npm run ui-lint
```

### Pointing at Another Instance
```bash
UI_LINT_BASE_URL=http://127.0.0.1:9000 npm run ui-lint
```

### Debugging a Failing Audit
- Read the runner output: it names the profile, the page and the specific check
- Inspect the written screenshots for the layout-stability failures
- Reproduce a single check by calling its `lib/` function from a scratch script, or run the matching `tests/js/ui-lint-*.test.mjs` to confirm whether the logic or the page changed

### Testing This Library's Logic
```bash
node --test tests/js/ui-lint-devices.test.mjs
node --test tests/js/ui-lint-axe.test.mjs
node --test tests/js/ui-lint-health.test.mjs
```

### CI Integration
`.github/workflows/ui-audit.yml` installs the toolchain with `npm ci --prefix tools/ui-lint` and runs the audit. `.github/workflows/ci.yml` runs `npm run lint:contracts`, which executes `check-source-contracts.mjs` from this directory without a browser.

---

**Last Updated:** 2026-09-21  
**Browser Automation:** Playwright (chromium, webkit, firefox)  
**Accessibility:** axe-core via `@axe-core/playwright`  
**Screenshot Diffing:** pixelmatch + pngjs  
**Invocation:** `npm run ui-lint` — not `npm test`
