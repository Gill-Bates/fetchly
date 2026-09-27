<!-- Parent: ../AGENTS.md -->

# app/templates/ — Jinja2 HTML Templates

**Purpose:** Server-rendered HTML using Jinja2, providing the dashboard, login, settings, job detail and share-error pages.

Templates here render the entire UI. There is no client-side framework and no compiled component tree: the server returns HTML, and the ES modules in `app/static/js/` progressively enhance it.

## Key Files

| File | Purpose |
| --- | --- |
| `base.html` | Base layout: document structure, navbar, stylesheet/script includes, CSRF token exposure, dark `color-scheme` meta |
| `index.html` | Dashboard: job submission form, URL input, format selection, job list |
| `login.html` | Login page: credentials form, invisible honeypot anti-bot field, error messages |
| `settings.html` | Settings: authentication, download tuning, watermark, cookies, Lalal.ai integration, System tiles (version/update, host resources) |
| `job.html` | Job detail: metadata, download controls, trim editor, stem separation |
| `share_error.html` | Shown when a share link is expired, invalid, or over its use limit |
| `_action_btn.html` | Action button partial. Currently **not included** by any page — the dashboard renders job rows client-side from `jobsBootstrapData`; `tests/test_status_mapping_parity.py` keeps its status sets in sync with the backend. |
| `macros/navbar.html` | Navigation bar macro |
| `macros/stats.html` | Host/statistics display macro |

## For AI Agents

### Working on Templates
- **Editing:** change the HTML; Jinja2 picks up template changes without a code restart
- **Styles:** classes resolve against `/static/style.css` (and `/static/login.css`); Bootstrap 5 utilities are available from the vendored bundle
- **Asset URLs:** reference stylesheets, scripts and the vendored bundle through `{{ asset_url('/static/…') }}` — it appends the file's content hash. Never write a `?v=` token by hand; `npm run lint:contracts` fails on one. The font `<link rel="preload">` tags in `base.html` are the documented exception (see `app/utils/assets.py`).
- **Interactivity:** add `data-*` hooks and handle them from an ES module under `/static/js/` — no inline `<script>` blocks, and no framework components
- **Syntax:** `{{ value }}`, `{% if %}`, `{% for %}`, `{% macro %}`
- **Inheritance:** pages extend `base.html`
- **Partials vs macros:** `_`-prefixed files are `{% include %}`-style partials; `macros/` holds `{% import %}`-able macros

### Common Jinja2 Patterns
- **Variables:** `<h1>{{ page_title }}</h1>`
- **Conditionals:** `{% if job.status == 'done' %}…{% endif %}` — use the real status names (`done`, `analysis_done`, `error`, `cancelled`, `queued`, `downloading`, `processing`, `transcoding`, `analysis`)
- **Loops:** `{% for job in jobs %}<li>{{ job.title }}</li>{% endfor %}`
- **Filters:** custom filters are registered from `app/utils/template_filters.py`
- **Macros:** `{% import 'macros/navbar.html' as navbar %}{{ navbar.render(...) }}`

### Adding a New Page
1. Create the `.html` file here and extend `base.html`
2. Override the content block
3. Add the route handler in `app/routes/` and return a `TemplateResponse`
4. Register the router in `app/main.py` if it is a new module
5. Add coverage under `tests/js/` for behavior, or a Python test for the rendered contract

### Working with Forms
- **CSRF:** state-changing requests under `/login`, `/logout` and `/api` are checked by `CSRFMiddleware`. Forms carry the token in a hidden `csrf_token` field; fetch calls send it as the `X-CSRF-Token` header. The header is set **per call site**, not centrally — `submitJob()` in `static/js/api.js` takes the token as an argument, and `settings.js`, `trim.js`, `main.js` and `login.js` each set the header on their own `fetch()` calls. A new mutating request must set it itself.
- **Validation:** use HTML5 input types for client-side hints; the server validates regardless
- **Error display:** render the error from the template context
- **Dialogs:** destructive confirmations use `confirmModal()` from `static/js/confirm.js`, never `window.confirm`

### Adding Dynamic Content
- **JavaScript hooks:** `data-job-id="{{ job.id }}"` and similar
- **Live updates:** the dashboard receives job updates over **SSE** (`/events`, consumed by `static/js/events.js`); the DOM is patched without a reload
- **Loading indicators:** toggle existing spinner elements from the module that owns the interaction

## Dependencies

### Internal
- `/app/static/` — CSS and ES modules loaded by `base.html`
- `/app/routes/` — handlers that render these templates
- `/app/utils/template_filters.py` — custom Jinja2 filters
- `/middleware/csrf.py` — issues the CSRF cookie whose token forms carry

### External
- **Jinja2:** template engine
- **Starlette templating:** `Jinja2Templates`, configured in `app/main.py`
- **Bootstrap 5:** vendored at `/static/vendor/bootstrap.min.css` and `bootstrap.bundle.min.js`
- **Fonts:** self-hosted from `/static/fonts/` (Material Symbols, Roboto Flex). No CDN links, no Google Fonts — the CSP does not allow them.

## Manual

### Running Templates Locally
```bash
python run.py           # Dev server on http://127.0.0.1:8000
```

### Testing Templates
- **UI behavior:** `tests/js/*.test.mjs` against the fake DOM in `tests/js/helpers/`
- **Rendered contracts:** Python tests such as `tests/test_mobile_settings_layout.py` and `tests/test_status_mapping_parity.py`
- **Source contracts:** `npm run lint:contracts` checks template/JS agreement
- **Visual/a11y audit:** `npm run ui-lint` (Playwright, separate from `npm test`)

### Template Context Variables
Passed by the route handlers; the ones that recur:
- `request` — required by `Jinja2Templates`
- `csrf_token` — token matching the CSRF cookie
- `job` / `jobs` — job record(s)
- `settings` — runtime settings map
- `error` — error message string

Check the specific handler in `app/routes/` for the exact context rather than assuming.

### Adding Custom Filters
1. Add the function to `app/utils/template_filters.py`
2. Register it where the filters are wired up in `app/main.py`
3. Use it as `{{ value|filter_name }}`
4. Cover it in `tests/test_template_filters.py`

### Debugging Template Issues
- **Syntax errors:** Jinja2 reports the template and line
- **Missing variables:** check the handler's context dict
- **CSRF failures:** confirm the hidden field or `X-CSRF-Token` header is present and the path is under a protected prefix
- **Styles not applying:** confirm the class exists in `style.css`; there is no `css/` directory

---

**Last Updated:** 2026-09-19  
**Template Engine:** Jinja2  
**CSS Framework:** Bootstrap 5 (vendored)  
**Live Updates:** Server-Sent Events
