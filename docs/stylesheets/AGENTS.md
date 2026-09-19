<!-- Parent: ../AGENTS.md -->

# docs/stylesheets/ — Documentation Theme Customization

**Purpose:** Custom CSS layered on top of Material for MkDocs: fetchly brand colors, dark-mode background matching, and styling for code blocks, admonitions, grid cards, tables and brand assets.

The stylesheet extends the Material theme rather than replacing it, so theme updates keep working.

## Key Files

| File | Purpose |
| --- | --- |
| `extra.css` | The entire customization. Registered via `extra_css:` in `docs/mkdocs.yml`. |

## For AI Agents

### How theming actually works here
Two things matter and both differ from a plain static site:

1. **Dark mode is driven by the Material palette toggle, not by the OS.** `docs/mkdocs.yml` declares two palettes — `scheme: default` (light) and `scheme: slate` (dark) — each with a toggle button. Material sets `data-md-color-scheme="default"` or `"slate"` on the document, and CSS keys off that attribute:

   ```css
   [data-md-color-scheme="slate"] {
       --md-default-bg-color: #0f172a;
   }
   ```

   `extra.css` contains **no `@media (prefers-color-scheme: dark)` query**, and adding one would not work: the visitor's manual toggle would be ignored, and the rule would fire against the wrong palette whenever OS preference and toggle disagree. Use the attribute selector.

2. **There are two families of custom properties, with different jobs.**
   - `--fy-primary` (`#5b5fdc`) and `--fy-accent` (`#818cf8`) — fetchly's own brand tokens, defined on `:root`. Use these for fetchly-specific accents.
   - `--md-*` — Material's own variables, *overridden* under `[data-md-color-scheme="slate"]` so the docs' dark background matches the application: `--md-default-bg-color` and its `--light`/`--lighter`/`--lightest` steps, `--md-code-bg-color`, `--md-footer-bg-color`, `--md-footer-bg-color--dark`.

   There are no `--color-primary`, `--color-text`, `--color-bg` or `--color-accent` variables. Inventing one and setting it changes nothing, because the theme never reads it.

### Existing customizations
| Selector | Purpose |
| --- | --- |
| `:root` | `--fy-primary`, `--fy-accent` |
| `[data-md-color-scheme="slate"]` | Dark background/code/footer colors matching the app |
| `.highlight` | Code block radius and spacing |
| `.md-typeset .admonition.tip` | Tip accent color from `--fy-accent` |
| `.grid.cards`, `.grid.cards > *`, `:hover` | Grid card layout and hover state |
| `.md-typeset .lg`, `.middle` | Icon sizing/alignment helpers |
| `.md-button` | Button styling |
| `.md-typeset table:not([class])`, `th` | Table styling |
| `.md-clipboard` | Copy-button styling |
| `.mermaid` | Diagram block styling |
| `.fy-logo-light`, `.fy-logo-dark` | Brand logo swap, switched under `[data-md-color-scheme="slate"]` |
| `.fy-platform-icon` | Platform icon rendering, adjusted for the slate scheme |

### Adding a Customization
1. Add the rule to `extra.css` — it is the only stylesheet, and `mkdocs.yml` loads exactly this one file
2. For anything color-related, decide which family it belongs to: a fetchly brand accent (`--fy-*`) or a Material override (`--md-*`)
3. If it needs a dark variant, add a companion rule under `[data-md-color-scheme="slate"]`
4. Preview both palettes with the toggle in the rendered site
5. Prefer overriding a Material variable over restyling a Material component — the variable survives theme upgrades, a hand-written component rule often does not

### Brand Assets
`.fy-logo-light` / `.fy-logo-dark` implement the logo swap against `docs/assets/img/fetchly_black.svg` and `fetchly_white.svg`. Keep both variants in sync when replacing branding.

## Dependencies

### Internal
- `/docs/mkdocs.yml` — registers this file under `extra_css:` and defines the two palettes that drive `[data-md-color-scheme]`
- `/docs/assets/img/` — the logo and platform icons these rules style

### External
- **Material for MkDocs:** supplies the `--md-*` variable set, the `data-md-color-scheme` attribute and the component classes being extended

## Manual

### Previewing Changes
```bash
mkdocs serve -f docs/mkdocs.yml
```
The config lives at `docs/mkdocs.yml` with `docs_dir: .`, so it must be passed explicitly — `mkdocs serve` from the repo root finds no config.

Toggle between light and dark with the palette switch in the header and check both.

### Finding the Right Variable
Material's variable names are documented upstream and visible in DevTools on any rendered page. Inspect the element, read which `--md-*` variable produces the color, then override that variable rather than the rule that consumes it.

### Common Pitfalls
- **Rule has no effect:** the variable name does not exist. Confirm it in DevTools before defining it.
- **Dark mode not applying:** the rule is under `@media (prefers-color-scheme: dark)` instead of `[data-md-color-scheme="slate"]`
- **Style lost after a theme upgrade:** a Material component class was restyled directly instead of overriding the variable behind it

---

**Last Updated:** 2026-09-19  
**Theme:** Material for MkDocs  
**Dark Mode:** `[data-md-color-scheme="slate"]`, driven by the palette toggle in `docs/mkdocs.yml`  
**Brand Tokens:** `--fy-primary`, `--fy-accent`
