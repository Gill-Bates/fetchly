<!-- Parent: ../AGENTS.md -->

# tools/ — Development Tooling

**Purpose:** Lint configuration, the source-contract checker, the Playwright UI audit, and a dependency exporter for `pyproject.toml`.

## Key Files

| File | Purpose |
| --- | --- |
| `eslint.config.mjs` | ESLint flat config: builds on `js.configs.recommended` plus repo-local rules |
| `stylelint.config.mjs` | Stylelint config, based on `stylelint-config-standard` |
| `pyproject-deps.py` | Prints one dependency group from `pyproject.toml` as a flat pip requirements list |

## Subdirectories

| Directory | Purpose |
| --- | --- |
| `ui-lint/` | Playwright-based UI audit: device profiles, axe-core accessibility, layout-shift detection, console severity, health metrics, plus the source-contract checker |

### ui-lint/ Details

| File | Purpose |
| --- | --- |
| `run-ui-lint.mjs` | The audit runner. Holds the `DEVICE_PROFILES` object, drives Playwright across browsers and viewports, and aggregates results. |
| `check-source-contracts.mjs` | Static source check: verifies the frontend and backend agree on shared vocabulary, and that the templates version `/static` URLs through `asset_url()` (run by `npm run lint:contracts`) |
| `lib/axe.mjs` | axe-core integration and tag selection |
| `lib/layout-shift.mjs` | Layout-shift measurement |
| `lib/console-severity.mjs` | Console message capture and severity classification |
| `lib/browser-utils.mjs` | Browser/page helpers |
| `lib/ui-health.mjs` | UI health metrics |
| `lib/source-contracts.mjs` | The contract rules `check-source-contracts.mjs` applies |
| `package.json` | Its own dependency set: `playwright`, `@axe-core/playwright`, `pixelmatch`, `pngjs`. Scripts: `audit` and `install:browsers` — there is no `test` script. |

## For AI Agents

### Working on Linting
- **ESLint:** edit `eslint.config.mjs`. Run it as CI does: `npm run lint:js` (which passes `--config tools/eslint.config.mjs`).
- **Stylelint:** edit `stylelint.config.mjs`. Run `npm run lint:css` — it needs both `--config tools/stylelint.config.mjs` and `--config-basedir .`, which is why the npm script exists rather than a bare `npx stylelint`.
- **Python:** `ruff check .` over the whole repo, as CI does. The rule set, the deliberate ignores and the per-file relaxations all live in `pyproject.toml`; read the comments there before disabling anything.
- **Everything at once:** `npm run lint` = `lint:js` + `lint:css` + `lint:contracts`. `npm run lint:fix` auto-fixes the first two.

### Working on the UI Audit
- **Device profiles:** the `DEVICE_PROFILES` object in `run-ui-lint.mjs` — there is no `lib/device-profiles.mjs`. Six profiles: `desktop` (1440x1200, chromium), `mobile` (iPhone 13, webkit), `tablet` (iPad Mini, webkit), `tablet-landscape` (iPad Mini landscape), `tablet-wide` (iPad Pro 11 landscape), `desktop-firefox` (1440x1200, firefox).
- **Form factor and touch are separate axes.** `formFactor` answers what kind of device it is; touch capability is independent. A narrow non-touch window is not a phone, and a touch iPad must not be handed the desktop touch-target minimum. Keep them separate when adding a profile.
- **`desktop-firefox` varies the engine, not the layout** — same viewport as `desktop` on purpose, so Gecko's differences in flexbox min-size, scrollbar gutters and subgrid surface with everything else held constant.
- **Adding a check:** add the logic to a `lib/*.mjs` module, call it from `run-ui-lint.mjs`, and unit-test the pure part from `tests/js/ui-lint-*.test.mjs`
- **Run it:** `npm run ui-lint` (first time: `npm run ui-lint:install` to fetch the browsers)

### Working on the Source-Contract Check
`check-source-contracts.mjs` compares the frontend sources against the backend vocabulary — most importantly the job status sets mirrored in `app/static/js/config.js` — and checks the invariants that no single file shows, such as a template writing a `?v=` token by hand instead of calling `asset_url()`. It runs as `npm run lint:contracts` and gates CI. If you rename a status in `app/db.py`, this is one of the three places that will fail until you update the mirror.

### Working on pyproject-deps.py
It is a **requirements exporter**, not a validator:

```bash
python tools/pyproject-deps.py                    # [project] dependencies
python tools/pyproject-deps.py dev                # the dev extra
python tools/pyproject-deps.py docs               # the docs extra
python tools/pyproject-deps.py --exclude torch --exclude beat-this --exclude essentia
```

Its consumers are pip's `-r`, Trivy's filesystem scanner, and the Docker builder (which installs dependencies without installing the project, so the layer stays cached across source-only changes). The rule that runtime dependencies carry no `==` pins is enforced elsewhere — `tests/test_version_source.py`.

## Dependencies

### Internal
- `../app/static/`, `../app/templates/` — the sources linted and audited
- `../tests/js/` — unit tests for the `ui-lint` library's pure logic
- `../pyproject.toml` — the dependency source `pyproject-deps.py` reads

### External
- **JavaScript:** ESLint 10, `@eslint/js`, `globals`, Stylelint 17, `stylelint-config-standard` (root `package.json`)
- **UI audit:** Playwright, `@axe-core/playwright`, `pixelmatch`, `pngjs` (`tools/ui-lint/package.json`)
- **Python:** 3.13+ for `pyproject-deps.py` (standard library only)

Playwright, not Puppeteer.

## Manual

### Linting Locally
```bash
ruff check .                # Python, whole repo — matches CI
ruff format .               # Python formatting

npm run lint                # ESLint + Stylelint + contracts
npm run lint:fix            # auto-fix JS and CSS
```

### Running the UI Audit Locally
```bash
npm run ui-lint:install     # once: installs chromium, webkit, firefox
npm run ui-lint             # node tools/ui-lint/run-ui-lint.mjs
```

### Adding a New Lint Rule
1. Add the rule to `eslint.config.mjs` or `stylelint.config.mjs`
2. Run `npm run lint`
3. Fix or justify every new violation in the same change — CI blocks on them

### Adding a Device Profile
Edit `DEVICE_PROFILES` in `tools/ui-lint/run-ui-lint.mjs`:

```javascript
'tablet-wide': {
    engine: 'webkit',
    formFactor: 'tablet',
    playwrightDevice: 'iPad Pro 11 landscape',
},
```

Use `playwrightDevice` for a named Playwright descriptor, or `viewport` for an explicit size (as `desktop` does). Then extend `tests/js/ui-lint-devices.test.mjs`.

---

**Last Updated:** 2026-09-21  
**Browser Automation:** Playwright (chromium, webkit, firefox)  
**Accessibility:** axe-core via `@axe-core/playwright`  
**Key Tools:** ESLint, Stylelint, ruff, Playwright
