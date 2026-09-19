<!-- Parent: ../AGENTS.md -->

# docs/getting-started/ — Installation and First-Run Guides

**Purpose:** User-facing documentation for installing fetchly and completing initial setup: Docker Compose quickstart, environment configuration, creating admin account, and first login.

Pages here guide new users from zero to a working fetchly instance, covering both containerized and bare-metal deployments.

## Key Files

| File | Purpose |
| --- | --- |
| `installation.md` | Installation methods: Docker Compose (recommended), bare metal Python, system requirements (Python 3.13+, FFmpeg, yt-dlp) |
| `docker.md` | Docker-specific setup: docker-compose.yml example, volume mounts, port mapping, environment variables, networking |
| `quick-start.md` | Quick start guide: five-minute setup, basic config, creating admin account, first job submission |
| `first-steps.md` | First steps tutorial: accessing web UI, submitting a download job, monitoring progress, downloading result |

## For AI Agents

### Writing Getting-Started Docs
- **Audience:** New users, non-technical to intermediate technical background
- **Scope:** Installation only; features covered in /features/ docs
- **Format:** Step-by-step with code examples, screenshots, troubleshooting
- **Testing:** Follow all steps yourself; verify instructions work end-to-end

### Common Sections
- **Prerequisites:** System requirements, dependencies, permissions
- **Installation:** Platform-specific steps (Docker, Linux, macOS, Windows)
- **Configuration:** Minimal required settings, optional settings for later
- **Verification:** How to verify installation succeeded (access web UI)
- **Next steps:** Link to feature guides and configuration docs

### Markdown Best Practices
- **Headings:** Use `#` for main sections, `##` for subsections
- **Code blocks:** Triple backticks with language (```bash, ```yaml)
- **Emphasis:** `**bold**` for UI elements, `*italic*` for emphasis
- **Links:** Internal `[text](../other-file.md)`, external `[text](https://url.com)`
- **Lists:** Unordered with `-`, ordered with `1.`, nested with indent

### Testing Examples
Every code example must be tested:
1. Run on fresh system (or virtual machine)
2. Follow exactly as written
3. Verify expected outcome
4. Document assumptions/prerequisites

## Dependencies

### Internal
- `/docs/` — Main docs site structure
- `/docs/mkdocs.yml` — Navigation hierarchy (the MkDocs config lives inside `docs/`, with `docs_dir: .`, not at the repo root)
- `../configuration/` — Detailed config docs (linked for advanced users)
- `../features/` — Feature guides (linked after setup)

### External
- **MkDocs:** Documentation generator
- **Docker:** For Docker installation examples
- **Python 3.13+:** For bare-metal examples

## Manual

### Adding Installation Method
1. Create section in `installation.md` or new file (e.g., `kubernetes.md`)
2. Include prerequisites, step-by-step instructions, verification
3. Test all commands on actual system
4. Add to `mkdocs.yml` navigation if new page
5. Link from quick-start guide

### Verifying Instructions
```bash
# Fresh test environment
docker run --rm -it ubuntu:latest bash
# Follow your instructions exactly
# Note any missing steps or incorrect commands
```

### Common Issues Section
```markdown
## Troubleshooting

### Docker container won't start
- Check logs: `docker-compose logs`
- Verify port not in use: `lsof -i :8000`
- Rebuild image: `docker-compose build --no-cache`
```

### Testing Docker Instructions
```bash
# Fresh checkout
git clone https://github.com/Gill-Bates/fetchly.git
cd fetchly
# Follow docker.md instructions exactly
docker-compose up
# Wait for "Uvicorn running" message
# Open http://localhost:8000
```

### Updating for New Version
1. Update version numbers in examples
2. Test installation with new version
3. Note breaking changes if any
4. Update configuration examples if settings changed
5. Test all examples before merging

---

**Last Updated:** 2026-09-19  
**Audience:** New users (non-technical to intermediate)  
**Scope:** Installation and basic setup only  
**Testing:** All examples verified on actual systems  
**Platforms:** Docker (recommended), Linux, macOS, Windows (WSL2)
