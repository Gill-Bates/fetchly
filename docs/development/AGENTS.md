<!-- Parent: ../AGENTS.md -->

# docs/development/ — Developer Guides and Architecture

**Purpose:** Documentation for developers contributing to fetchly: local setup, code structure, testing, extending the application, and architectural design decisions.

Pages here enable developers to understand how fetchly is built, set up a development environment, write tests, and add new features.

## Key Files

| File | Purpose |
| --- | --- |
| `setup.md` | Local development setup: virtual environment, dependency installation, database initialization, running dev server |
| `architecture.md` | Architecture overview: component design, data flow, decision rationale (SQLite vs. Postgres, async workers, etc.) |
| `contributing.md` | Contribution guidelines: code style, testing requirements, PR process, commit message format, sign-off procedures |

## For AI Agents

### Writing Development Documentation
- **Audience:** Software developers, moderate to advanced technical level
- **Scope:** Development, testing, extension; deployment covered in configuration docs
- **Format:** Conceptual with code examples, architecture diagrams, workflow descriptions
- **Testing:** Follow setup instructions on clean system; verify all commands work

### Development Setup Section
- Prerequisites: Python 3.13+ (`requires-python` in `pyproject.toml`), Node 22 (pinned in CI), FFmpeg, yt-dlp
- Virtual environment creation
- Dependency installation (pip, npm)
- Database setup
- Running dev server
- Verification (access dashboard, run tests)

### Code Style and Standards
Document project conventions:
- **Python:** PEP 8, type hints, docstrings
- **JavaScript:** vanilla ES modules, no build step and no npm runtime dependency; Bootstrap 5 is vendored under `app/static/vendor/` and loaded by `app/templates/base.html`, so "no framework" means no framework *build*, not no framework. `data-` attributes for DOM selectors
- **Naming:** Descriptive variable/function names, CamelCase for classes, snake_case for functions
- **Comments:** Explain why, not what; use docstrings for public APIs

### Testing Guidelines
- **Unit tests:** Isolated component behavior
- **Integration tests:** Feature workflows across modules
- **UI tests:** DOM rendering, accessibility, responsive layout
- **Coverage:** reported, not enforced. `[tool.coverage.run]` in `pyproject.toml` covers `app` and `middleware` with branch coverage, and `[tool.coverage.report]` sets `show_missing`. There is no `fail_under` and CI sets no threshold — do not invent one.
- **CI gate:** `.github/workflows/ci.yml` runs `ruff check`, `pytest -q`, `npm run lint:js`, `npm run lint:css`, `npm run lint:contracts` and `npm test`. All must pass before merge.

### Common Development Workflows
- **Adding feature:** Plan → Create branch → Code → Test → PR → Review → Merge
- **Fixing bug:** Reproduce → Write failing test → Fix code → Verify test passes → PR
- **Performance improvement:** Measure baseline → Optimize → Benchmark improvement → PR with results

## Dependencies

### Internal
- `/app/` — Backend code structure being documented
- `/tests/` — Test suite examples
- `/tools/ui-lint/` — Testing infrastructure
- `../getting-started/` — Basic setup (linked for prerequisites)

### External
- **MkDocs:** Documentation generator
- **Python 3.13+:** For development examples
- **Git:** For version control examples

## Manual

### Setting Up Development Environment
```bash
# Clone repository
git clone https://github.com/Gill-Bates/fetchly.git
cd fetchly

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .[dev]     # Installs with dev dependencies
npm install               # Install JavaScript test dependencies

# Initialize database
python run.py             # Creates/migrates database on startup

# Verify setup
pytest tests/ -v          # Run Python tests
npm test                  # Run JavaScript tests
```

### Running Tests
```bash
# All tests
pytest && npm test

# Specific test file
pytest tests/test_auth_flow.py
node --test tests/js/confirm-modal.test.mjs

# With coverage (reported, not gated). Requires the dev extra -
# pytest-cov is not in the base install and CI never runs coverage.
# [tool.coverage.run] already scopes it to app + middleware, so do not
# narrow it back down with --cov=app.
pytest --cov
```

### Adding New Feature
1. Create branch: `git checkout -b feature/description`
2. Implement feature with tests
3. Run test suite: `pytest && npm test && npm run lint`
4. Commit: `git commit -m "feat: description"`
5. Push and create PR
6. Wait for review and CI to pass

### Code Review Checklist
- [ ] Tests written and passing
- [ ] No console errors or warnings
- [ ] Type hints added (Python)
- [ ] Docstrings added for public APIs
- [ ] No unused imports
- [ ] Code follows project style
- [ ] Performance regression considered

### Debugging Tips
```bash
# Python debugging
pytest tests/test_file.py -vv -s --pdb  # Drop to debugger on failure

# JavaScript debugging
node --test tests/js/toast.test.mjs                      # single file
node --test --test-name-pattern "renders" tests/js/      # filter by test name
node --test --watch tests/js/toast.test.mjs              # re-run on save
# node:test has no --grep - use --test-name-pattern. --watch does exist
# (Node 19+). npm test is `node --test "tests/js/*.test.mjs"`.

# Server debugging
LOG_LEVEL=DEBUG python run.py
# run.py installs no FileHandler - logs go to stdout/stderr only, so there is
# no app.log to tail. Redirect if you need one: python run.py > run.log 2>&1
```

### Performance Profiling
```bash
# Python profiling
python -m cProfile -s cumulative run.py > profile.txt

# Check database performance
# Enable query logging in db.py
# Monitor slow queries
```

---

**Last Updated:** 2026-09-19  
**Language:** Python 3.13+, JavaScript (ES6+)  
**Test Framework:** pytest (Python), `node --test` with `node:assert/strict` (JavaScript)  
**Version Control:** Git with GitHub  
**Code Style:** ruff (Python), ESLint `js.configs.recommended` plus local rules (JavaScript)
