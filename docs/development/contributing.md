# Contributing

fetchly is open to issues and pull requests on
[GitHub](https://github.com/Gill-Bates/fetchly).

## Before you start

For anything beyond a small fix, open an issue first. It saves both sides the work of a
pull request built on an approach that turns out not to fit — especially for anything
touching authentication, the CSRF/anti-bot flow, or the settings allow-list, where the
constraints are easy to miss from the outside.

## Development setup

See [Development Setup](setup.md) for the environment, running the app locally, and
running the test suite.

## Making a change

1. Fork the repository and branch from `main`
2. Make the change
3. Add or update tests — see below
4. Run the test suite locally
5. Open a pull request against `main`

## Code conventions

- **Explain the why, not just the what.** The codebase's docstrings and comments lean
  heavily on *why a design choice was made* — a constraint, a past incident, a
  trade-off — because that is the part a diff cannot show. Match that when you touch a
  module.
- **Settings go through the allow-list.** A new runtime setting needs an entry in
  `_SETTINGS_DEFAULTS` and `_SETTINGS_TYPES` in `app/db.py`, with a parser that
  range-checks the value server-side — never trust client-side validation alone.
- **No native browser dialogs.** Use the shared `confirmModal()` in
  `app/static/js/confirm.js` instead of `confirm()`/`alert()`/`prompt()`.
- **Rate limits are part of the route.** A new state-changing endpoint needs a
  `@limiter.limit(...)` decorator sized to what the endpoint costs and what abuse of it
  would achieve — see [Rate Limiting](../security/rate-limiting.md) for the existing
  scale.
- **No CDN dependencies.** Front-end libraries are vendored under
  `app/static/vendor/`, which is what keeps the reference reverse-proxy CSP at
  `script-src 'self'`.

## Tests

| Kind | Location | Run with |
|---|---|---|
| Python | `tests/test_*.py` | `pytest` |
| Front-end contracts | `tests/js/*.test.mjs` | Node, no browser |

A change to shared policy modules — `bpm_normalization.py`, `bpm_naming.py`,
`lalal_policy.py`, `public_url.py`, the settings parsers in `db.py` — should come with
a test, since these are exactly the modules other parts of the app depend on without
re-checking their invariants.

## Security-sensitive areas

Changes here get closer scrutiny, since a subtle regression is easy to miss in review
and expensive once shipped:

- `middleware/csrf.py`
- `app/session.py`
- `app/routes/auth.py`
- `app/utils/hidden_captcha.py`
- `app/common/rate_limit.py`
- `app/utils/public_url.py` (host validation)

If your change touches how a request is authenticated, how a cookie is issued, or how a
trust boundary (`FORWARDED_ALLOW_IPS`, the CSRF token, the anti-bot check) is
evaluated, say so explicitly in the pull request description — reviewers should not
have to infer it from the diff.

## Reporting a security issue

Do not open a public issue for a vulnerability. Use
[GitHub Security Advisories](https://github.com/Gill-Bates/fetchly/security/advisories)
instead.

## Documentation

This documentation site lives under `docs/` and is built with MkDocs Material. If your
change affects behaviour a user or operator would need to know about — a new setting, a
changed default, a new environment variable — update the relevant page in the same pull
request.

```bash
pip install -r docs/requirements-docs.txt
mkdocs serve -f docs/mkdocs.yml
```

Then open `http://127.0.0.1:8000`. Pages rebuild on save.

## License

By contributing, you agree that your contribution is licensed under the project's
[AGPL-3.0 license](https://github.com/Gill-Bates/fetchly/blob/main/LICENSE).
