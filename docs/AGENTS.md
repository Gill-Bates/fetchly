<!-- Parent: ../AGENTS.md -->

# docs/ — Documentation

**Purpose:** MkDocs Material-based user and developer documentation covering installation, API, configuration, security, troubleshooting, and development guides.

The documentation site is the single source of truth for users and developers: how to run fetchly, configure it, integrate it with external services (Lalal.ai), handle platform cookies, understand the REST API, and troubleshoot common issues.

## Key Files

| File | Purpose |
| --- | --- |
| `mkdocs.yml` | MkDocs configuration: site title, theme, palettes, plugins, navigation. Lives *inside* `docs/` with `docs_dir: .`, not at the repo root — every command must pass `-f docs/mkdocs.yml` |
| `index.md` | Documentation homepage: overview, feature highlights, quick links to major sections |
| `troubleshooting.md` | Troubleshooting guide: common issues (job stuck, login fails, cookies rejected) and solutions |

## Subdirectories

| Directory | Purpose |
| --- | --- |
| `getting-started/` | `installation.md`, `docker.md`, `quick-start.md`, `first-steps.md` |
| `configuration/` | `environment.md`, `settings.md`, `storage.md`, `resources.md`, `reverse-proxy.md` (singular). `environment.md` is the authoritative env-var reference — link to it, never restate its table |
| `api/` | `overview.md`, `endpoints.md`, `authentication.md` |
| `features/` | `downloads.md`, `jobs.md`, `bpm.md`, `trimming.md`, `stems.md`, `cookies.md`, `sharing.md` |
| `security/` | `overview.md`, `authentication.md`, `anti-bot.md`, `rate-limiting.md`, `best-practices.md` |
| `development/` | `setup.md`, `architecture.md`, `contributing.md` |
| `assets/` | Static assets for docs (images, diagrams, screenshots) |
| `stylesheets/` | Custom CSS for documentation site theme customization |

## For AI Agents

### Working on User Documentation
- **Adding a guide:** Create `.md` file in appropriate subdirectory. Check the existing filename first — the reverse-proxy page is `configuration/reverse-proxy.md`, singular
- **Markdown style:** Use Markdown 1.0 headings (# for h1, ## for h2); code blocks with triple backticks and language (```bash, ```python)
- **MkDocs features:** Admonitions (`!!! note "Title"` for info boxes), tabs for platform-specific instructions, collapsible sections (`??? example`)
- **Images:** Place in `assets/img/`; reference as `![alt text](../assets/img/screenshot.png)` (relative to doc file)
- **Cross-linking:** Use `[text](../other-section/page.md)` or `[text](index.md#anchor)`
- **Update nav:** Edit `mkdocs.yml` to add new page to navigation tree

### Working on API Documentation
- **Endpoint docs:** Create file in `api/` (e.g., `api/jobs.md` for job endpoints)
- **Format:** Use tables for parameters, code blocks for example curl commands and JSON responses
- **Test examples:** Verify that curl/HTTP examples work against running server before documenting
- **Versioning:** Note if endpoint is new in a specific version (e.g., "Added in v1.2.0")

### Working on Developer Guides
- **Setup guide:** `development/setup.md` covers local environment, dependencies, virtual env
- **Architecture:** `development/architecture.md` — key design decisions (SQLite, async workers for job processing)
- **Contributing:** `development/contributing.md` — code style, PR process
- **Testing:** there is no `development/testing.md`. Test invocation is documented in `development/AGENTS.md` and `setup.md`; add the page before linking to it

### Building and Deploying Docs
- **Local preview:** `mkdocs serve -f docs/mkdocs.yml` (runs on http://127.0.0.1:8000)
- **Build static site:** `mkdocs build -f docs/mkdocs.yml` generates the `site/` directory
- **Deployment:** GitHub Pages, via `.github/workflows/docs-build.yml` on push to `main` (path-filtered to `docs/**`, `CHANGELOG.md`, `LICENSE` and the workflow itself)
- **Theme:** Material for MkDocs (https://squidfunk.github.io/mkdocs-material/); customization via `mkdocs.yml` and `stylesheets/`

### Common Patterns
- **Callout blocks:** Use admonitions for warnings, tips, notes
  ```markdown
  !!! warning "Important"
      This action cannot be undone.
  ```
- **Code blocks with highlighting:**
  ```python
  # Python code highlighted
  import fetchly
  ```
- **Tabs for platform differences:**
  ```markdown
  === "Linux"
      docker run fetchly
  === "macOS"
      docker run --platform linux/amd64 fetchly
  ```
- **Collapsible sections:**
  ```markdown
  ??? example "View example"
      Content here, collapsed by default
  ```

## Dependencies

### Internal
- `/app/` — Backend code being documented
- `/tests/` — Test examples sometimes included in docs
- `../pyproject.toml` — Project version (referenced in getting-started)
- `../CHANGELOG.md` — Release notes linked from docs

### External
- **MkDocs:** mkdocs (site generator), mkdocs-material (theme), mkdocs-git-revision-date-localized-plugin (last-updated dates), mkdocs-minify-plugin (optimize HTML), mkdocs-redirects (handle old URLs)
- **Python:** Python 3.13+ to run mkdocs
- **CI/CD:** GitHub Pages (hosting), `.github/workflows/docs-build.yml` (auto-deploy on main push)

## Manual

### Building Docs Locally
```bash
pip install -e .[docs]              # Install MkDocs and plugins
mkdocs serve -f docs/mkdocs.yml     # Live preview on http://127.0.0.1:8000
```
The config lives at `docs/mkdocs.yml` with `docs_dir: .`, so it must be passed explicitly — `mkdocs serve` from the repo root finds no config.

### Building Static Site
```bash
mkdocs build -f docs/mkdocs.yml
# Generates site/ directory
# Push to GitHub Pages or serve with nginx
```

### Adding a New Documentation Page
1. Create `.md` file in appropriate subdirectory (e.g., `docs/configuration/new-feature.md`)
2. Write content using Markdown
3. Update the `nav:` tree in `docs/mkdocs.yml` (paths are relative to `docs/`):
   ```yaml
   nav:
     - Home: index.md
     - Configuration:
       - Settings: configuration/settings.md
       - New Feature: configuration/new-feature.md  # Add here
   ```
4. Run `mkdocs serve -f docs/mkdocs.yml` to preview
5. Commit and push to main (GitHub Actions auto-deploys)

### Updating API Documentation
1. Edit/create file in `docs/api/`
2. Test all curl examples against running server
3. Update the endpoint path, parameters, response structure if you change the backend
4. Include error examples (400, 401, 500 responses)
5. Note version introduced (e.g., "Available since v1.2.0")

### Troubleshooting Docs Build
```bash
mkdocs build -f docs/mkdocs.yml --verbose    # Show detailed build output
# Common issues:
# - Broken links: mkdocs reports invalid markdown links
# - Missing images: check image paths are relative to doc file
# - Plugin errors: ensure all plugins listed in mkdocs.yml are installed
```

### Viewing Last-Updated Dates
- Plugin `mkdocs-git-revision-date-localized-plugin` adds last-modified timestamp to each page footer
- Requires git history; not shown on `mkdocs serve` (only in built site)
- `changelog.md` and `license.md` are generated at build time (copied from `CHANGELOG.md` / `LICENSE`), so their git history does not match the page path. They are listed under the plugin's `exclude:` and fall back to the build date. Without that, the plugin emits a "first revision timestamp is older than last" warning that fails the `--strict` build. Add any other build-time-copied page to that list.

---

**Last Updated:** 2026-09-19  
**Site Generator:** MkDocs Material  
**Hosted:** GitHub Pages (https://gill-bates.github.io/fetchly/)  
**Build Trigger:** Push to `main` via `.github/workflows/docs-build.yml`  
**Config Location:** `docs/mkdocs.yml` (`docs_dir: .`) — pass `-f docs/mkdocs.yml` to every mkdocs command
