<!-- Parent: ../AGENTS.md -->

# docs/assets/ — Documentation Assets

**Purpose:** Static assets for documentation: logos, icons, screenshots, diagrams, and social media images used in MkDocs pages.

Assets here are referenced by documentation files via relative paths (e.g., `![alt](../assets/img/screenshot.png)`). They include both fetchly branding and platform integration graphics (YouTube, TikTok, Instagram, Facebook logos).

## Key Files and Directories

| Item | Purpose |
| --- | --- |
| `favicon.svg` | Browser favicon: displayed in browser tabs and bookmarks |
| `img/` | Main images directory: screenshots, logos, diagrams |
| `img/fetchly_black.svg` | Fetchly logo (black variant): used in dark mode or neutral backgrounds |
| `img/fetchly_white.svg` | Fetchly logo (white variant): used on dark backgrounds |
| `img/screen_1.jpeg`, `screen_2.jpeg` | Screenshots of fetchly dashboard and features: used in getting-started docs |
| `img/lalal_ai.svg` | Lalal.ai service logo: used in stem separation documentation |
| `img/social/` | Social media icons: YouTube, TikTok, Instagram, Facebook (light/dark variants) |
| `img/social/*_black.svg`, `*_white.svg` | Platform logos in different color schemes for theme compatibility |

## For AI Agents

### Adding Images to Documentation
1. Place image file in `assets/img/` (organized by feature if many files)
2. Reference in markdown: `![description](../assets/img/filename.png)`
3. Use descriptive alt text for accessibility
4. Optimize file size before adding (compress PNG/JPEG, minify SVG)

### Image Guidelines
- **Format:** PNG for photos/screenshots, SVG for logos/icons, JPEG for large photos
- **Size:** Optimize for web (max 500KB per image)
- **Resolution:** 2x for retina displays if applicable
- **Accessibility:** Always include descriptive alt text
- **Naming:** Use lowercase, hyphens for spaces (my-screenshot.png)

### SVG Logo Guidelines
- **Variants:** Provide light/dark versions for theme support
- **Optimization:** Use `svgo` tool to minimize file size
- **Viewbox:** Use explicit viewBox for scaling
- **Colors:** Use CSS variables for theme support if embedded in docs

### Adding Social Media Icons
1. Create logo pair: `platform_black.svg` and `platform_white.svg`
2. Place in `img/social/`
3. Reference in docs based on theme
4. Test in light and dark modes

## Dependencies

### Internal
- `/docs/` — Documentation files that reference these assets
- `/docs/mkdocs.yml` — Configuration specifies asset directories (the MkDocs config lives inside `docs/`, with `docs_dir: .`, not at the repo root)

### External
- **MkDocs:** Serves static assets via docs site
- **Image optimization:** Optional (svgo for SVG, imagemin for raster)

## Manual

### Adding Screenshots
```bash
# Take screenshot, crop to relevant area
# Optimize: convert screenshot.png -quality 85 screenshot-optimized.png
# Place in docs/assets/img/
# Reference in markdown: ![Dashboard](../assets/img/screenshot.png)
```

### Optimizing SVG Logos
```bash
# Install svgo: npm install -g svgo
# Optimize: svgo logo.svg -o logo-optimized.svg
# Check output quality before committing
```

### Testing Assets in Documentation
```bash
cd /opt/fetchly
mkdocs serve                    # Preview at http://localhost:8000
# Navigate to pages using assets
# Verify images load correctly in light and dark modes
```

### Adding Platform Logo
1. Create or source logo in SVG format
2. Optimize with `svgo`
3. Save as `platform_black.svg` and `platform_white.svg` in `img/social/`
4. Test in docs with both themes active
5. Commit both variants

---

**Last Updated:** 2026-09-19  
**Formats:** SVG (logos), PNG (screenshots), JPEG (photos)  
**Color Schemes:** Light/dark variants for theme support  
**Optimization:** svgo for SVG, imagemin for raster formats
