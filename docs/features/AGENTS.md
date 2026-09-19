<!-- Parent: ../AGENTS.md -->

# docs/features/ — Feature Guides

**Purpose:** User-facing documentation for fetchly features: downloading media, analyzing BPM, trimming audio, separating stems, sharing content, and watermarking.

Pages here explain how to use each major feature, with screenshots, examples, and troubleshooting.

## Key Files

| File | Purpose |
| --- | --- |
| `downloads.md` | Media download feature: supported platforms (YouTube, TikTok, Instagram, Facebook), format selection, quality options, download progress, errors |
| `jobs.md` | Job management: submitting jobs, monitoring progress, canceling jobs, retrying failed jobs, viewing job history |
| `bpm.md` | BPM detection: the two-detector cascade, preprocessing, octave normalization, the `_94bpm` tag on the download filename, cached results, performance impact |
| `trimming.md` | Audio trimming: waveform editor, setting trim points, listening to clips, saving trimmed audio |
| `stems.md` | Stem separation: Lalal.ai integration, stem types (vocals, drums, bass, other), quality levels, pricing, results |
| `cookies.md` | Cookie management: why cookies needed, importing from browser, platform-specific requirements, troubleshooting |
| `sharing.md` | Share links: generating public links, expiration/usage limits, accessing shared media without login, revoking access |

## For AI Agents

### Writing Feature Guides
- **Audience:** End users, non-technical to intermediate
- **Scope:** How to use feature, what it does, limitations; configuration in setup docs
- **Format:** Step-by-step with screenshots, examples, common questions
- **Testing:** Use actual fetchly instance; take real screenshots; verify all steps work

### Feature Guide Structure
1. **Overview:** What feature does, why useful
2. **Prerequisites:** What user needs before starting
3. **Step-by-step:** Numbered instructions with screenshots
4. **Examples:** Real-world usage examples
5. **Troubleshooting:** Common issues and fixes
6. **Related:** Links to related features and config

### Markdown Best Practices
- **Headings:** Use `##` for main sections, `###` for subsections
- **Images:** Screenshots showing UI state at each step
- **Code/UI elements:** Use monospace for file paths, code, UI text
- **Bold:** Emphasize button labels: **Download**, **Submit**
- **Lists:** Use `-` for bullets, `1.` for numbered steps

### Testing Feature Documentation
1. Start fresh fetchly instance
2. Follow guide step-by-step exactly as written
3. Take screenshots at each step
4. Note any missing steps or unclear instructions
5. Verify result matches description

## Dependencies

### Internal
- `/docs/` — Main docs site structure
- `/docs/mkdocs.yml` — Navigation hierarchy (the MkDocs config lives inside `docs/`, with `docs_dir: .`, not at the repo root)
- `../getting-started/` — Linked for initial setup
- `../configuration/` — Linked for feature-specific config
- `../api/` — API endpoints called by features

### External
- **MkDocs:** Documentation generator
- **Screenshots:** Taken from actual fetchly instance
- **Fetchly instance:** For testing features

## Manual

### Adding New Feature Guide
1. Create `.md` file in `docs/features/` (e.g., `new-feature.md`)
2. Use template from existing guide (e.g., `trimming.md`)
3. Write step-by-step instructions with actual UI
4. Take screenshots: use consistent browser/viewport size
5. Test guide on fresh instance
6. Add to `mkdocs.yml` navigation
7. Link from related guides

### Taking Screenshots
```bash
# Consistent setup
# Use full HD browser window (1920x1080 or similar)
# Use light theme for consistency
# Show relevant UI only (crop if needed)
# Include cursor if pointing at something
# Save as PNG in docs/assets/img/features/
```

### Writing Clear Instructions
```markdown
## Submitting a Download Job

### Step 1: Enter Media URL
1. Click the **URL** field at the top of the page
2. Paste the YouTube video link: `https://www.youtube.com/watch?v=...`
3. Press **Tab** or click elsewhere to load video info

![Entering URL field](../assets/img/features/download-1-url.png)

The dashboard will show video title and available formats.
```

### Accessibility in Guides
- **Alt text:** Describe what screenshot shows, not "screenshot"
- **Headings:** Use proper hierarchy (don't skip levels)
- **Emphasis:** Use `**bold**` instead of ALL CAPS
- **Links:** Descriptive text instead of "click here"

### Common Issues to Document
```markdown
## Troubleshooting

### Job stays in "Queued" state
- Check if another job is running (YouTube limit: 2 concurrent)
- Wait for first job to complete
- If stuck >5 minutes, refresh page or restart fetchly

### Download fails with "403 Forbidden"
- Video may be geo-restricted or age-gated
- Some platforms require cookies (see [Cookie Setup](../features/cookies.md))
```

### Performance Notes
- Document expected times (download duration varies by size)
- Note resource usage (BPM analysis uses CPU)
- Mention Lalal.ai quota limits for stem separation

---

**Last Updated:** 2026-09-19  
**Audience:** End users (non-technical to intermediate)  
**Scope:** Feature usage and troubleshooting  
**Screenshots:** From actual fetchly instance  
**Testing:** All steps verified with real features
