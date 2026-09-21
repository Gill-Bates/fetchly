<!-- Parent: ../AGENTS.md -->

# middleware/ — Request Middleware

**Purpose:** Double-submit-cookie CSRF protection for state-changing requests on the login, logout, and `/api` path prefixes.

This module implements the CSRF layer that sits in the ASGI middleware stack and validates that state-changing requests carry a token matching the client's CSRF cookie.

## Key Files

| File | Purpose |
| --- | --- |
| `csrf.py` | `CSRFMiddleware`: token generation, cookie issuance, token comparison, rejection responses |

There is no `__init__.py` in this directory, although `pyproject.toml` declares `middleware` as a package.

## For AI Agents

### Working on CSRF Protection
- **Registration:** `app/main.py` adds it to the stack with `csrf_cookie_name` and `protected_paths=("/login", "/logout", "/api")`
- **Allowlist, not denylist:** `protected_paths` is an **opt-in** list of prefixes that get checked. The constructor rejects an empty tuple and rejects entries that do not start with `/`. There is no `CSRF_EXEMPT_PATHS`. To exempt a route, keep it outside the protected prefixes; to protect a new prefix, add it here.
- **Token delivery:** the middleware sets the CSRF cookie when the request does not already carry one
- **Validation:** state-changing requests must present the token in the `X-CSRF-Token` header or a `csrf_token` form field, matching the cookie
- **Error handling:** a failed check returns `403` with a JSON body

### The cookie is deliberately readable by JavaScript
`csrf.py` sets `httponly=False`, with an explicit comment: *"Intentionally omit HttpOnly so JavaScript can read the token."* That is not an oversight — it is what double-submit requires, because the frontend has to read the cookie to put the token into the `X-CSRF-Token` header. **Do not "harden" this to `httponly=True`**; it breaks every fetch call in `app/static/js/api.js`. The protection against XSS in this design comes from `SameSite` plus the origin checks, not from `HttpOnly`.

### Common Patterns
- **Form submission:** hidden field `<input type="hidden" name="csrf_token" value="…">`
- **API calls:** `app/static/js/api.js` reads the cookie and sets the `X-CSRF-Token` header
- **Cookie lifetime:** the token persists in the cookie; it is not regenerated on every request or after every successful POST
- **Secure flag:** follows `FETCHLY_BEHIND_HTTPS`

## Dependencies

### Internal
- Imported by `../app/main.py` and added to the middleware stack

### External
- **Starlette:** `ASGIApp` from `starlette.types` and `MutableHeaders` from `starlette.datastructures`
- **FastAPI:** `Request` and `JSONResponse` for the rejection response
- **Standard library only** for tokens: `secrets` (generation *and* the constant-time comparison via `secrets.compare_digest`), `re` for cookie-name validation, `urllib.parse` for form bodies. No `hmac`, and the `cryptography` package is not a project dependency.

## Manual

### CSRF Token Flow
1. Request arrives for a protected prefix without a CSRF cookie → middleware generates a token and sets the cookie
2. Template renders the token into a hidden field; JavaScript reads the cookie for header-based calls
3. State-changing request presents the token in the header or form body
4. Middleware compares it against the cookie → match proceeds, mismatch returns `403`

### Testing CSRF Validation
Tests use the unittest-style helpers in `tests/_support.py` (`WebAppTestCase`), not pytest fixtures:

```python
from tests._support import WebAppTestCase


class CsrfTest(WebAppTestCase):
    def test_post_without_token_is_rejected(self):
        response = self.client.post("/api/submit", json={"url": "https://example.com"})
        self.assertEqual(response.status_code, 403)
```

See `tests/js/csrf-token.test.mjs` for the client-side contract.

### Protecting a New Path Prefix
Edit the `protected_paths` tuple where `app/main.py` adds the middleware:

```python
app.add_middleware(
    CSRFMiddleware,
    csrf_cookie_name=_CSRF_COOKIE,
    protected_paths=("/login", "/logout", "/api"),
)
```

### Debugging CSRF Issues
- **403 on form submission:** confirm the hidden `csrf_token` field is present and matches the cookie
- **403 on fetch:** confirm `X-CSRF-Token` is set; confirm the cookie is readable (it must not be `HttpOnly`)
- **Cookie missing entirely:** the path is outside `protected_paths`, so no cookie was ever issued
- **Mismatch after a proxy change:** check `SameSite` and the `Secure` flag against `FETCHLY_BEHIND_HTTPS`

---

**Last Updated:** 2026-09-21  
**Standard:** Double-Submit Cookie Pattern  
**Cookie:** `SameSite` set, `Secure` when behind HTTPS, `HttpOnly` deliberately off  
**Scope:** `/login`, `/logout`, `/api` prefixes
