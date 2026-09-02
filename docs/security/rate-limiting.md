# Rate Limiting

Every route carries a per-client limit, enforced by
[SlowAPI](https://github.com/laurentS/slowapi) and keyed on the client IP.

## Client identification

The limiter keys on the client IP, taken from `X-Forwarded-For` **only** when the
request arrives from an address listed in `FORWARDED_ALLOW_IPS`.

!!! danger "A wildcard is rejected at startup"
    If any caller's forwarded headers were trusted, every limit could be bypassed by
    rotating a header value. `FORWARDED_ALLOW_IPS=*` is refused rather than accepted —
    set your proxy's address or CIDR. The default is `127.0.0.1,::1`.

Configuring it wrongly is not a silent problem either: every request then appears to
come from the proxy, so all clients share one bucket and the first busy user locks out
everyone else. See [Reverse Proxy](../configuration/reverse-proxy.md).

## Limits by route

### Authentication

| Route | Limit |
|---|---|
| `GET /login` | 20/minute |
| `POST /login` | **5/minute** |
| `POST /logout` | 20/minute |

The login limit is the backstop behind the [invisible anti-bot check](anti-bot.md).

### Jobs

| Route | Limit |
|---|---|
| `POST /api/submit` | 10/minute |
| `GET /api/jobs` | 60/minute |
| `GET /api/jobs/{id}` | 60/minute |
| `POST /api/jobs/{id}/cancel` | 30/minute |
| `POST /api/jobs/{id}/retry` | 10/minute |
| `POST /api/jobs/remove-all` | **2/minute** |

### Media

| Route | Limit |
|---|---|
| `GET /job/{id}` | 120/minute |
| `GET /download/{id}` | 30/minute |
| `GET /audio-source/{id}` | 60/minute |
| `GET /thumbnail/{id}` | 120/minute |
| `GET /api/thumbnail-cache/{key}` | 120/minute |
| `GET /api/thumbnail/resolve` | 30/minute |
| `GET /api/thumbnail-proxy` | 30/minute |

### Settings and system

| Route | Limit |
|---|---|
| `GET /api/settings` | 60/minute |
| `POST /api/settings` | **5/minute** |
| `GET /api/stats` | 30/minute |
| `POST /api/stats/reset` | 5/minute |
| `GET /api/stats/bpm-clusters` | 30/minute |
| `GET /api/updates` | 30/minute |
| `GET /api/system/host` | 60/minute |
| `GET /api/info` | 20/minute |

### Events

| Route | Limit |
|---|---|
| `GET /events` | 30/minute |
| `GET /api/jobs/{id}/events` | 30/minute |

Independently of the rate limit, at most **200** concurrent SSE connections are
accepted, with a 32-event queue per client. A client that cannot keep up is
disconnected rather than allowed to grow its queue without bound.

### Cookies

| Route | Limit |
|---|---|
| `GET /api/cookies` | 30/minute |
| `POST /api/cookies/{platform}` | 10/minute |
| `POST /api/cookies/{platform}/paste` | 10/minute |
| `DELETE /api/cookies/{platform}` | 10/minute |

### Trimming

| Route | Limit |
|---|---|
| `POST /api/trim/{id}` | 10/minute |
| `DELETE /api/trim/{id}` | 30/minute |
| `GET /api/trim/{id}/{trim_id}/download` | 30/minute |

### Lalal.ai

| Route | Limit |
|---|---|
| `GET /api/lalal/status` | 30/minute |
| `POST /api/lalal/auth/activation-key` | **5/minute** |
| `POST /api/lalal/auth/logout` | 10/minute |
| `POST /api/lalal/{job_id}` | **5/minute** |
| `GET /api/lalal/download/{job_id}` | 10/minute |

### Share links

| Route | Limit |
|---|---|
| `POST /api/share/{job_id}` | 20/minute |
| `GET /share/{token}` | 20/minute |

!!! info "The redeem limit is a security control"
    Share tokens are 8 characters (48 bits). Brute force is bounded by this limit
    rather than by token length, so raising it materially weakens share links.

## Exceeding a limit

The response is `429 Too Many Requests`. The UI surfaces it as a toast; API clients
should back off and retry.

## Tuning

The limits are compiled into the route decorators and are not configurable at runtime —
they encode a security posture rather than a capacity setting. If a limit genuinely
blocks a legitimate workflow, open an issue rather than working around it with a proxy
rule; the numbers reflect what each route costs and what abuse of it would achieve.

## What it does not cover

Rate limiting bounds request frequency, not resource consumption. A single accepted
download can still occupy a worker for an hour. Concurrency and memory are governed
separately — see [Resources & Workers](../configuration/resources.md).
