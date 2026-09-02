# API Authentication

fetchly has one authentication mechanism: the session cookie. The API and the web UI
share it.

!!! info "No API tokens"
    There is no bearer token, no API key, and no separate machine scheme. A single-admin
    application with no per-object ownership has nothing a second credential type would
    buy.

## Two modes

| Authentication | Behaviour |
|---|---|
| Disabled (default) | Every endpoint is open; requests run as the internal identity `local` |
| Enabled | Every endpoint except the public ones requires a valid session cookie |

Public regardless of the setting:

| Route | Why |
|---|---|
| `GET /health` | Orchestrator probes |
| `GET /login`, `POST /login` | The login itself |
| `GET /share/{token}` | Share links target people without accounts |
| `GET /static/*` | Assets |

## Logging in

`POST /login` takes a **JSON** body and is CSRF-protected.

```json
{
  "username": "admin",
  "password": "...",
  "captcha_token": "<from the login page>",
  "honeypot": ""
}
```

| Field | Required | Notes |
|---|---|---|
| `username` | yes | Letters, hyphens, and underscores only; at most 64 characters |
| `password` | yes | 8–1024 characters |
| `captcha_token` | yes, when authentication is on | Rendered into the login page as a hidden input |
| `honeypot` | yes, and must be empty | The field name comes from the page; leaving it out entirely also passes |

Extra fields are **rejected** — the model forbids them.

Response:

```json
{"ok": true, "redirect": "/"}
```

with `Set-Cookie: fetchly_session=...`.

| Failure | Status | Body |
|---|---|---|
| Anti-bot check failed | `400` | Generic "couldn't verify your submission" |
| Wrong credentials | `401` | `Invalid credentials` |
| CSRF failure | `403` | `detail` explains |
| Too many attempts | `429` | Rate limited (5/minute) |

!!! warning "The anti-bot check has a minimum age"
    A login submitted less than **1 second** after the page was served is rejected. A
    script must fetch `/login` first, and wait. Tokens stay valid for 6 hours and are
    not single-use.

## CSRF

`/login`, `/logout`, and `/api/submit` are protected by a double-submit cookie.

1. `GET /login` sets the `fetchly_csrf` cookie and embeds the same value in the page
2. Send it back in the `X-CSRF-Token` header, or as a `csrf_token` form field
3. A mismatch returns `403`

Other API routes are not CSRF-protected: they are JSON-only endpoints that a
cross-origin form cannot forge, and the `SameSite=Lax` session cookie is not attached to
cross-site requests.

## Using the session

```bash
curl -b cookies.txt https://fetchly.example.com/api/jobs
```

The cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` when
`FETCHLY_BEHIND_HTTPS=1` or the request arrives over HTTPS.

## Session lifetime

| Limit | Value |
|---|---|
| Hard | 24 hours from login, not configurable |
| Idle | Sliding, `session_idle_minutes` (1–1440, default 60) |

Both are enforced server-side and reflected in the cookie's `Max-Age`. A client that
sits idle past the timeout gets `401`/`403` and must log in again.

## Logging out

```bash
curl -b cookies.txt -X POST -H "X-CSRF-Token: $CSRF" https://fetchly.example.com/logout
```

## A complete script

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE="https://fetchly.example.com"
USER="admin"
PASS="..."
JAR="$(mktemp)"
PAGE="$(mktemp)"
trap 'rm -f "$JAR" "$PAGE"' EXIT

# Fetch the login page: CSRF cookie + anti-bot token
curl -sc "$JAR" "$BASE/login" -o "$PAGE"
CSRF="$(awk '$6=="fetchly_csrf"{print $7}' "$JAR")"
CAPTCHA="$(grep -oP 'name="captcha_token"[^>]*value="\K[^"]+' "$PAGE")"

# The time trap rejects anything faster than a second
sleep 1.5

curl -sb "$JAR" -c "$JAR" -X POST "$BASE/login" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d "$(jq -nc --arg u "$USER" --arg p "$PASS" --arg c "$CAPTCHA" \
        '{username:$u, password:$p, captcha_token:$c, honeypot:""}')" \
  >/dev/null

# Authenticated from here on
curl -sb "$JAR" "$BASE/api/jobs?limit=5" | jq .
```

!!! tip "Scripting an instance you control"
    On a private network with nothing untrusted able to reach the port, leaving
    authentication off removes the login flow entirely. That is a deliberate trade —
    see [Best Practices](../security/best-practices.md).

## Rate limits on auth routes

| Route | Limit |
|---|---|
| `GET /login` | 20/minute |
| `POST /login` | 5/minute |
| `POST /logout` | 20/minute |

Limits are per client IP, so `FORWARDED_ALLOW_IPS` must name your proxy or every client
shares one bucket. See [Rate Limiting](../security/rate-limiting.md).
