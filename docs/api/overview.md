# API Overview

fetchly exposes a JSON HTTP API. It is the same API the web UI uses — there is no
separate surface and no second authentication scheme.

## Base URL

```text
https://fetchly.example.com
```

Routes are absolute; there is no shared `/api` prefix for every family (`/download/{id}`
and `/share/{token}` sit at the root).

## Authentication

The API uses the **session cookie**, exactly like the browser. There is no API token
and no bearer scheme. See [API Authentication](authentication.md).

When authentication is disabled, every endpoint is open.

## OpenAPI

The app is built on FastAPI, so the generated schema is available at the framework
defaults:

```text
https://fetchly.example.com/docs           # Swagger UI
https://fetchly.example.com/redoc          # ReDoc
https://fetchly.example.com/openapi.json   # Raw schema
```

!!! warning "These are not public endpoints"
    They are gated by the same session as everything else once authentication is on. Do
    not expose them anonymously.

## Endpoint families

| Family | Purpose |
|---|---|
| `/health` | Unauthenticated health probe |
| `/login`, `/logout` | Session lifecycle |
| `/api/submit`, `/api/jobs/*` | Create, list, cancel, retry, and remove jobs |
| `/api/info` | Resolve metadata for a URL before submitting |
| `/events`, `/api/jobs/{id}/events` | Server-Sent Event streams |
| `/download/{id}`, `/audio-source/{id}`, `/thumbnail/{id}` | Media delivery |
| `/api/thumbnail/*`, `/api/thumbnail-proxy` | Thumbnail resolution and caching |
| `/api/trim/*` | Audio trimming |
| `/api/lalal/*` | Lalal.ai auth and stem separation |
| `/api/cookies/*` | Per-platform cookie import and status |
| `/api/share/{id}`, `/share/{token}` | Share link creation and redemption |
| `/api/settings` | Read and write runtime settings |
| `/api/stats`, `/api/stats/*` | Dashboard statistics |
| `/api/updates`, `/api/system/host` | Version checks and host resources |

The full list is in [Endpoints](endpoints.md).

## Content types

| Direction | Type |
|---|---|
| Most requests | `application/json` |
| `POST /api/submit` | `application/x-www-form-urlencoded` |
| Cookie file upload | `multipart/form-data` |
| SSE responses | `text/event-stream` |
| Media responses | The file's own type |

### CSRF

`/login`, `/logout`, and `/api/submit` are CSRF-protected with a double-submit cookie
(`fetchly_csrf`). Send the token back either in the `X-CSRF-Token` header or, for form
bodies, as a `csrf_token` field. A mismatch returns `403`.

## Status codes

| Code | Meaning |
|---|---|
| `200` | Success |
| `303` | Redirect after a successful form login |
| `400` | Validation error — the message says what |
| `401` / `403` | No valid session, or a CSRF failure |
| `404` | Not found, or deliberately indistinguishable (share links) |
| `409` | Duplicate job; the existing job is attached |
| `413` | Upload too large |
| `429` | Rate limit exceeded |
| `503` | Job queue full |

Errors are FastAPI's shape:

```json
{"detail": "Invalid type. Allowed: audio, video"}
```

## Rate limits

Every route is limited per client IP. See [Rate Limiting](../security/rate-limiting.md).

## Example session

```bash
BASE=https://fetchly.example.com
JAR=$(mktemp)

# 1. Fetch the login page: sets the CSRF cookie and embeds the anti-bot token
curl -sc "$JAR" "$BASE/login" -o /tmp/login.html
CSRF=$(awk '$6=="fetchly_csrf"{print $7}' "$JAR")
CAPTCHA=$(grep -oP 'name="captcha_token"[^>]*value="\K[^"]+' /tmp/login.html)

# 2. Log in. POST /login takes JSON; wait ~1 s, because a submission that
#    arrives faster than that trips the time trap.
sleep 1
curl -sb "$JAR" -c "$JAR" -X POST "$BASE/login" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d "{\"username\":\"admin\",\"password\":\"...\",\"captcha_token\":\"$CAPTCHA\",\"honeypot\":\"\"}"

# 3. Use the session
curl -sb "$JAR" "$BASE/api/jobs?limit=10"

# 4. Submit a download (form-encoded, CSRF-protected)
curl -sb "$JAR" -X POST "$BASE/api/submit" \
  -H "X-CSRF-Token: $CSRF" \
  -d "url=https://www.youtube.com/watch?v=..." \
  -d "type=audio" -d "quality=max"
```

!!! tip "Automating against an instance you control"
    If the instance is on a private network and only ever driven by scripts, leaving
    authentication off removes the login dance entirely. Only do that where nothing
    untrusted can reach the port.

## Live updates

Prefer SSE over polling:

```bash
curl -Nb "$JAR" "$BASE/events"
```

The stream emits job state changes plus a keep-alive every 5 seconds. Buffering proxies
break it — see [Reverse Proxy](../configuration/reverse-proxy.md).
