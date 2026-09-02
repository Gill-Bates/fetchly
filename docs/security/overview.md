# Security Overview

fetchly is built to be exposed to a network you do not fully control — but only after
you have turned authentication on.

## Threat model

fetchly is a **single-identity** application. There is at most one credential and no
per-job ownership, so authentication is the whole authorization story: whoever holds
the session sees every job. Adding more accounts would require an owner column and
per-job filtering before that stops being true.

What the design assumes:

| Assumption | Consequence |
|---|---|
| One admin, no roles | A session is full access |
| The data volume is trusted storage | Cookie jars and the Lalal.ai key live there in usable form |
| TLS is terminated by a proxy | The app itself speaks plain HTTP |
| Exactly one application process | The queue and event broker are in-memory |

## Defense in depth

| Layer | Mechanism |
|---|---|
| Authentication | Optional login; PBKDF2-HMAC-SHA256, 200,000 iterations, per-username salt and a key-derived pepper |
| Sessions | HMAC-signed cookie, `HttpOnly`, `SameSite=Lax`, `Secure` behind HTTPS |
| Session lifetime | 24-hour hard limit plus a sliding idle timeout |
| CSRF | Double-submit cookie on state-changing routes |
| Anti-bot | Invisible honeypot plus a signed time-trap token on the public login |
| Rate limiting | Per-route, per-client-IP limits on every endpoint |
| Proxy trust | Explicit `FORWARDED_ALLOW_IPS`; wildcards rejected |
| Input validation | Pydantic models, allow-listed settings keys, server-side range checks |
| Path safety | Job paths resolved against the data directory; traversal rejected |
| Share links | 48-bit tokens, use limits, one indistinguishable failure response |
| Container | Non-root runtime user, root-owned read-only application code, `no-new-privileges` |

## The fresh-install state

!!! danger "A new instance is unauthenticated"
    Authentication is **off** on first start and there is no built-in account. Anyone
    who can reach the port can queue downloads, read every finished file, import
    cookies, and spend your Lalal.ai minutes.

    Bind to `127.0.0.1`, create an admin account under **Settings → Security**, and
    only then expose it.

This is deliberate — there is no default password to leak and no bootstrap credential
to forget to change — but it puts the first step on you.

## Password storage

```text
salt   = SHA-256(FETCHLY_SECRET_KEY : username : "salt")
pepper = SHA-256(FETCHLY_SECRET_KEY : "pepper")
hash   = PBKDF2-HMAC-SHA256(password + pepper, salt, 200_000)
```

Only the hash is stored. Verification uses a constant-time comparison on both the
username and the hash.

!!! warning "The secret key is part of the hash"
    Salt and pepper are derived from `FETCHLY_SECRET_KEY`. Rotating the key invalidates
    the stored password — you would have to reset the credentials afterwards. Treat the
    key as permanent for the life of the instance.

## Sessions

| Property | Value |
|---|---|
| Cookie name | `fetchly_session` |
| Signing | HMAC over the payload with `FETCHLY_SECRET_KEY` |
| Flags | `HttpOnly`, `SameSite=Lax`, `Secure` when `FETCHLY_BEHIND_HTTPS=1` or the request is HTTPS |
| Hard limit | 24 hours from login, not configurable |
| Idle timeout | Sliding, `session_idle_minutes` (1–1440, default 60) |
| Invalidation | Bumping `session_version` invalidates every existing session at once |

Cookie `Max-Age` is set to whichever expiry comes first, so the browser drops the cookie
at the same moment the server stops honouring it.

## CSRF

A double-submit cookie protects the state-changing routes reachable from a browser
form — `/login`, `/logout`, and `/api/submit`. The token is random per issue and must
be echoed back in the request.

## Anti-bot on login

The public `POST /login` carries an invisible check with no user interaction: a
CSS-hidden honeypot field and an HMAC-signed time-trap token. See
[Anti-Bot Protection](anti-bot.md).

## Rate limiting

Every route carries a limit, keyed on client IP. The strictest sit on login (5/minute),
settings writes (5/minute), Lalal.ai operations (5/minute), and bulk deletion
(2/minute). See [Rate Limiting](rate-limiting.md).

## Secrets handling

| Secret | Storage | Exposure |
|---|---|---|
| `FETCHLY_SECRET_KEY` | Environment | Never rendered or logged |
| Admin password | Database, hashed | Never returned by any endpoint |
| Lalal.ai activation key | Database | Excluded from `GET /api/settings` |
| Platform cookies | `DATA_DIR/cookies/` (dir `0700`), files mode `0600` | Never rendered; a warning is logged if permissions are loose |

## Container hardening

- Application code is copied **root-owned** and normalized to `a+rX`; only `/app/data`
  is writable, so a compromised process cannot rewrite its own modules, templates, or
  the JavaScript served to browsers
- The entrypoint starts as root only long enough to fix volume ownership, then drops to
  `appuser` (UID 1000) via `gosu`
- `no-new-privileges:true` is set in the shipped Compose file
- No CDN: Bootstrap and wavesurfer.js are vendored, which is what lets the example
  Caddy CSP use `script-src 'self'`

## Reporting a vulnerability

Report privately through [GitHub Security Advisories](https://github.com/Gill-Bates/fetchly/security/advisories)
rather than in a public issue.

## Next

<div class="grid cards" markdown>

-   :material-account-key: **[Authentication](authentication.md)**
-   :material-robot-off: **[Anti-Bot Protection](anti-bot.md)**
-   :material-speedometer: **[Rate Limiting](rate-limiting.md)**
-   :material-check-decagram: **[Best Practices](best-practices.md)**

</div>
