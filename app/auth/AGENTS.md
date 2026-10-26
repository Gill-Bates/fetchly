<!-- Parent: ../AGENTS.md -->

# app/auth/ — Sessions, Credentials and Rate Limiting

**Purpose:** Authentication and request-trust concerns: HMAC session tokens, admin credential normalization, trusted-proxy validation, client-IP resolution, and the in-house sliding-window rate limiter.

This package exists so the proxy-trust rules are defined once and used identically by the rate limiter, the proxy middleware and the route decorators.

## Key Files

| File | Purpose |
| --- | --- |
| `session.py` | Session tokens signed with HMAC-SHA256, absolute lifetime (`session_max_days`), login state |
| `credentials.py` | Admin username normalization and password validation |
| `rate_limit.py` | Trusted proxy parsing and validation, client-IP resolution behind a proxy, and the `rate_limit()` sliding-window dependency factory |

## For AI Agents

### Working with Rate Limiting
- **Registration:** add `dependencies=[Depends(rate_limit("5/minute"))]` to the route decorator of sensitive endpoints (login, uploads, expensive queries)
- **Wiring:** none in `app/main.py`; the dependency raises `HTTPException(429, "Rate limit exceeded")` with a `Retry-After` header (seconds, at least 1). An internal limiter error is logged and also answers `429` (fail closed)
- **Trusted proxies:** configured with `FORWARDED_ALLOW_IPS` (comma-separated IPs or CIDRs), default `127.0.0.1,::1`. There is no `FETCHLY_TRUSTED_PROXIES`.
- **Validation is strict:** `_parse_trusted_proxy_specs()` raises on an unparsable entry, on an empty list, and on `*` — wildcard proxy trust is rejected outright, because it would let any client spoof its IP and defeat per-IP limiting
- **Client IP:** `OriginalClientMiddleware` stashes the raw peer under the `fetchly.original_client` scope key *before* `ProxyHeadersMiddleware` rewrites it, so the limiter can distinguish the real peer from a forwarded value
- **Caching:** parsed proxy specs are memoized with `functools.cache` for the process lifetime — changing the env var requires a restart

### Storage backend
Rate-limit counters are held **in memory only**. There is no Redis support: no Redis client is imported, and no backend environment variable is read. A deployment that needs shared counters across processes would have to add that support first — and note that `WORKERS` must stay at `1` anyway, so there is only ever one process holding them.

### Common Patterns
- **Syntax:** `rate_limit("10/minute")` (`second`, `minute` or `hour`; an invalid spec raises at import) — per client IP; every call creates its own `SlidingWindow`, so each route has its own budget. `MAX_KEYS` caps tracked clients per window (only expired keys are evicted; a full table rejects). Tests call `reset_rate_limits()`
- **Request argument:** none needed; the dependency reads the `Request` itself
- **Error response:** `429 Too Many Requests`
- **Middleware order matters:** `ProxyHeadersMiddleware` is added before `OriginalClientMiddleware` in `main.py`, because Starlette wraps the most recently added middleware outermost

## Dependencies

### Internal
- Imported by `/app/routes/` for endpoint limits
- Imported by `/app/main.py` for proxy-host validation

### External
- No third-party rate-limit library
- **Standard library:** `ipaddress` for network parsing, `functools.cache` for memoization

## Manual

### Enabling Rate Limiting on an Endpoint
```python
from fastapi import APIRouter, Depends

from ..auth.rate_limit import rate_limit

router = APIRouter()


@router.post("/api/sensitive-endpoint", dependencies=[Depends(rate_limit("5/minute"))])
async def sensitive_endpoint():
    return {"ok": True}
```

### Configuring Trusted Proxies
```bash
export FORWARDED_ALLOW_IPS="10.0.0.0/8,127.0.0.1,::1"
```

```nginx
# The reverse proxy must set the header the app then trusts:
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

Gunicorn in the container reads the same variable, so proxy trust is configured once for both layers.

### Debugging Rate Limit Issues
1. **Everything limited as one client:** the proxy IP is not in `FORWARDED_ALLOW_IPS`, so every request looks like it comes from the proxy
2. **App refuses to start:** an unparsable entry or `*` in `FORWARDED_ALLOW_IPS` — both raise on startup by design
3. **Limits not resetting:** counters are in-process memory; restarting clears them
4. **Changed the env var, nothing happened:** the parsed specs are cached for the process lifetime; restart

---

**Last Updated:** 2026-10-26  
**Rate Limiting:** in-house sliding window  
**Storage:** In-memory only  
**Proxy Trust:** `FORWARDED_ALLOW_IPS`, wildcards rejected
