<!-- Parent: ../AGENTS.md -->

# tests/js/ — JavaScript Test Suite

**Purpose:** `node:test` suites covering frontend module behavior and the pure logic of the UI-audit library.

These run in plain Node.js with no browser. Two groups live here: tests of `app/static/js/` modules against a fake DOM, and unit tests of the helpers in `tools/ui-lint/` that the Playwright audit uses. The audit itself is not run from here — see `tools/ui-lint/`.

## Key Files

| File | Purpose |
| --- | --- |
| `toast.test.mjs` | Toast notifications: creation, deduplication, timer scheduling, and the auto-dismiss callback through to removal (both the `transitionend` path and the fallback timer) |
| `confirm-modal.test.mjs` | Confirmation dialog: buttons, callbacks, focus handling, and the `data-bs-dismiss` wiring that makes close/cancel resolve as cancelled |
| `csrf-token.test.mjs` | Client-side CSRF contract: `getCsrfToken()` cookie/form/meta precedence, plus `submitJob()` sending the token as the `X-CSRF-Token` header |
| `cookie-paste-modal.test.mjs` | Cookie import UI: paste validation, format detection, errors |
| `watermark-logo.test.mjs` | Logo validation: SVG inspection, PNG inspection, rasterization, size limits |
| `limited-playback.test.mjs` | Playback restriction logic: clip boundaries and duration limits |
| `safe-redirect.test.mjs` | Redirect safety: origin validation against a stubbed `window.location` |
| `format-helpers.test.mjs` | Format description parsing and quality/codec naming. Self-contained — it stubs `document` and does not import an `app/static/js/format-helpers.js` module (no such file exists). |
| `config-contract.test.mjs` | Reads `app/static/js/config.js` as text, re-imports it per case with a stubbed `documentElement.dataset`, and asserts both the Lalal duration bootstrap contract and the exported status sets |
| `ui-lint-axe.test.mjs` | Unit tests for `tools/ui-lint/lib/axe.mjs` (tag selection, violation handling, and both failure modes via the injectable `loadAxeBuilder` loader) |
| `ui-lint-devices.test.mjs` | Unit tests for the device-profile logic in `tools/ui-lint/run-ui-lint.mjs` |
| `ui-lint-element-ids.test.mjs` | Unit tests for `markupElementIds` / `unresolvedElementIds` in `tools/ui-lint/lib/source-contracts.mjs`: ids a script looks up must exist in the markup, ids a script creates must not be reported |
| `ui-lint-result-summary.test.mjs` | Unit tests for `formatResultSummary` in `tools/ui-lint/run-ui-lint.mjs`: count metrics must print as numbers, and a metric that was never measured must not be reported as a finding |
| `ui-lint-layout-shift.test.mjs` | Unit tests for `tools/ui-lint/lib/layout-shift.mjs` |
| `ui-lint-console-severity.test.mjs` | Unit tests for `tools/ui-lint/lib/console-severity.mjs` |
| `ui-lint-health.test.mjs` | Unit tests for `tools/ui-lint/lib/ui-health.mjs` |
| `ui-lint-network-findings.test.mjs` | `isEventStreamCancellation` in `tools/ui-lint/run-ui-lint.mjs`: each browser engine's wording for a cancelled `/events` stream must not count as an application network failure, while a stream that genuinely could not connect still must |
| `helpers/fake-dom.mjs` | `installFakeDom({ withBootstrap })` — the shared fake DOM used by the frontend tests |

## For AI Agents

### Writing Tests
Framework: **`node:test` + `node:assert/strict`**. No vitest, no jest, no @testing-library, no `expect()`, no `describe()`.

```javascript
import assert from "node:assert/strict";
import test, { afterEach } from "node:test";

import { installFakeDom } from "./helpers/fake-dom.mjs";

const { body } = installFakeDom({ withBootstrap: false });

afterEach(() => {
    body.innerHTML = "";
});

test("shows an error toast", () => {
    showError("boom");
    assert.equal(body.querySelector(".toast-error").textContent, "boom");
});
```

- **Async:** `test("…", async () => { await … })`
- **Setup/teardown:** `beforeEach` / `afterEach` imported from `node:test`
- **Stubbing globals:** assign to `globalThis` directly (`globalThis.window = { … }`), as `safe-redirect.test.mjs` does
- **Fake timers:** there is no built-in fake-timer API in use; `toast.test.mjs` swaps `setTimeout` for a local registry and asserts on scheduling
- **Importing ui-lint internals:** `await import("../../tools/ui-lint/lib/axe.mjs")` — dynamic import, because those modules are ESM outside the root package

### What is *not* tested here
No browser is launched. Accessibility, layout shift, console severity and device rendering are *exercised* against real pages only by the Playwright audit in `tools/ui-lint/` (`npm run ui-lint`). The `ui-lint-*.test.mjs` files here test that library's pure functions — its tag lists, thresholds, profile resolution and scoring — so a logic regression is caught without spinning up browsers.

### Testing the frontend/backend status contract
`config-contract.test.mjs` reads `app/static/js/config.js` as source text and re-imports it as a data URL per case, so each test supplies its own `document.documentElement.dataset`. A case for a *missing* bootstrap attribute must leave the key off the dataset entirely — `String(undefined)` produces the string `"undefined"`, which tests an unparseable value a second time instead of an absent one. Its Python counterpart is `tests/test_status_mapping_parity.py`, and `npm run lint:contracts` performs the same class of check over the sources. Change a status in `app/db.py` and all three must be updated together.

### Adding a New Test File
1. Create `feature-name.test.mjs` here
2. Import `node:assert/strict` and `node:test`
3. Import `./helpers/fake-dom.mjs` if the module under test touches the DOM
4. Run `npm test`
5. Run `npm run lint:js` — ESLint covers the test files too

## Dependencies

### Internal
- `/app/static/js/` — the modules under test
- `/tools/ui-lint/` — the audit library whose pure logic is unit-tested here
- `helpers/fake-dom.mjs` — the shared DOM stub

### External
Node.js built-ins only. This directory pulls in no npm packages: axe-core and Playwright are dependencies of `tools/ui-lint/`, installed separately under that prefix.

## Manual

### Running Tests
```bash
npm test                                                   # node --test "tests/js/*.test.mjs"
node --test tests/js/toast.test.mjs                        # a single file
node --test --test-name-pattern "toast" tests/js/*.test.mjs # filter by test name
node --test --watch tests/js/*.test.mjs                    # watch mode
```

The `npm test` script has the glob baked in, so arguments appended after `--` do not filter it — invoke `node --test` directly when you need to narrow the run. `--grep` is not a `node:test` flag; the equivalent is `--test-name-pattern`.

### Coverage
```bash
node --test --experimental-test-coverage tests/js/*.test.mjs
```

No coverage threshold is enforced for JavaScript in CI.

### Debugging Failed Tests
```bash
node --test tests/js/confirm-modal.test.mjs     # full output for one file
```
`console.log()` inside a test is printed with the test's output.

### CI Integration
`.github/workflows/ci.yml` runs `npm test` in the lint/test job alongside ESLint, Stylelint and the source-contract check. The Playwright audit runs separately in `.github/workflows/ui-audit.yml`.

---

**Last Updated:** 2026-09-19  
**Framework:** `node:test` + `node:assert/strict`  
**Runtime:** Node.js, no browser  
**Files:** 17 test suites + `helpers/fake-dom.mjs`
