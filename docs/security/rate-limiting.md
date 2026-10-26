# Rate Limiting

Every route that costs work carries a per-client limit, enforced by an
in-house sliding-window limiter and keyed on the client IP (IPv6: the /64). The
dashboard (`GET /`), the settings page (`GET /settings`), `GET /favicon.ico` and the
`GET /health` probe carry no limit — they render a template or return a constant, and
the data behind them is fetched by the `/api/*` routes below, which are limited.

## Client identification

The limiter keys on the client IP, taken from `X-Forwarded-For` **only** when the
request arrives from an address listed in `FORWARDED_ALLOW_IPS`.

!!! danger "A wildcard is rejected at startup"
    If any caller's forwarded headers were trusted, every limit could be bypassed by
    rotating a header value. `FORWARDED_ALLOW_IPS=*` is refused rather than accepted —
    set your proxy's address or CIDR. The default is `127.0.0.1,::1`.

IPv4 clients get one bucket per address. IPv6 clients get one bucket per **/64**
prefix: a subscriber normally controls a whole /64, so rotating addresses inside it
would otherwise mint a fresh bucket per request. An IPv4-mapped IPv6 address
(`::ffff:a.b.c.d`) counts as the IPv4 address it wraps. Only the limiter key is
aggregated; a job's stored requester IP stays the full address.

Configuring it wrongly is not a silent problem either: every request then appears to
come from the proxy, so all clients share one bucket and the first busy user locks out
everyone else. See [Reverse Proxy](../configuration/reverse-proxy.md).

## Design guarantees

- **Per client, never per account.** A limit is keyed on the client IP (IPv6: the /64)
  plus the route, and on nothing a client can choose: no username, session or
  header/body value. fetchly has no account lockout by design — with a single admin
  credential, a per-account counter would let anyone lock the administrator out.
- **One client cannot throttle another.** Flooding from one address only exhausts that
  address's own budget on that route. Only requests that are admitted are recorded, so
  hammering never extends a block beyond the window.
- **Bounded memory without lockout.** Each route tracks at most 10,000 clients. When the
  table is full, expired entries are dropped first, then the least recently used one; a
  flood of fresh addresses can at worst reset other callers' counters, never reject them.
- **Untrusted forwarded headers are ignored.** `X-Forwarded-For` only counts when the
  socket peer is in `FORWARDED_ALLOW_IPS`, so a client cannot pick someone else's bucket.

!!! warning "Shared addresses share a bucket"
    Clients behind the same NAT or proxy appear as one address and share one budget. Behind
    a reverse proxy, `FORWARDED_ALLOW_IPS` must contain the proxy; otherwise every user
    shares the proxy's bucket and a single abusive client throttles everybody.
    The proxy must also send `X-Forwarded-For` (or `X-Real-IP`); without it the client
    cannot be told apart from the proxy.

## Limits by route

### Authentication

| Route          | Limit        |
| -------------- | ------------ |
| `GET /login`   | 20/minute    |
| `POST /login`  | **5/minute** |
| `POST /logout` | 20/minute    |

The login limit is the backstop behind the [invisible anti-bot check](anti-bot.md).
It is per client (IPv4 address or IPv6 /64), not per account, and fetchly has no
account lockout: with a single admin credential, a lockout would let anyone lock the
administrator out by failing logins on purpose. A botnet spread over many networks
is bounded only by the password strength, so use a long, unique admin password.

### Jobs

| Route                        | Limit        |
| ---------------------------- | ------------ |
| `POST /api/submit`           | 10/minute    |
| `GET /api/jobs`              | 60/minute    |
| `GET /api/jobs/{id}`         | 60/minute    |
| `POST /api/jobs/{id}/cancel` | 30/minute    |
| `POST /api/jobs/{id}/retry`  | 10/minute    |
| `POST /api/jobs/remove-all`  | **2/minute** |

### Media

| Route                            | Limit      |
| -------------------------------- | ---------- |
| `GET /job/{id}`                  | 120/minute |
| `GET /download/{id}`             | 30/minute  |
| `GET /audio-source/{id}`         | 60/minute  |
| `GET /thumbnail/{id}`            | 120/minute |
| `GET /api/thumbnail-cache/{key}` | 120/minute |
| `GET /api/thumbnail/resolve`     | 30/minute  |
| `GET /api/thumbnail-proxy`       | 30/minute  |

### Settings and system

| Route                                    | Limit        |
| ---------------------------------------- | ------------ |
| `GET /api/settings`                      | 60/minute    |
| `POST /api/settings`                     | **5/minute** |
| `GET /api/settings/watermark-logo`       | 60/minute    |
| `GET /api/settings/watermark-logo/image` | 60/minute    |
| `POST /api/settings/watermark-logo`      | 10/minute    |
| `DELETE /api/settings/watermark-logo`    | 10/minute    |
| `GET /api/stats`                         | 30/minute    |
| `POST /api/stats/reset`                  | 5/minute     |
| `GET /api/stats/bpm-clusters`            | 30/minute    |
| `GET /api/updates`                       | 30/minute    |
| `GET /api/system/host`                   | 60/minute    |
| `GET /api/info`                          | 20/minute    |

### Events

| Route                       | Limit     |
| --------------------------- | --------- |
| `GET /events`               | 30/minute |
| `GET /api/jobs/{id}/events` | 30/minute |

Independently of the rate limit, at most **200** concurrent SSE connections are
accepted, with a 32-event queue per client. A client that cannot keep up is
disconnected rather than allowed to grow its queue without bound.

### Cookies

| Route                                | Limit     |
| ------------------------------------ | --------- |
| `GET /api/cookies`                   | 30/minute |
| `POST /api/cookies/{platform}`       | 10/minute |
| `POST /api/cookies/{platform}/paste` | 10/minute |
| `DELETE /api/cookies/{platform}`     | 10/minute |

### Trimming

| Route                                   | Limit     |
| --------------------------------------- | --------- |
| `POST /api/trim/{id}`                   | 10/minute |
| `DELETE /api/trim/{id}`                 | 30/minute |
| `GET /api/trim/{id}/{trim_id}/download` | 30/minute |

### Lalal.ai

| Route                                 | Limit        |
| ------------------------------------- | ------------ |
| `GET /api/lalal/status`               | 30/minute    |
| `POST /api/lalal/auth/activation-key` | **5/minute** |
| `POST /api/lalal/auth/logout`         | 10/minute    |
| `POST /api/lalal/{job_id}`            | **5/minute** |
| `GET /api/lalal/download/{job_id}`    | 10/minute    |

### Share links

| Route                      | Limit     |
| -------------------------- | --------- |
| `POST /api/share/{job_id}` | 20/minute |
| `GET /share/{token}`       | 20/minute |

!!! info "The redeem limit backs up the token length"
    Share tokens carry 128 bits of randomness, so guessing one is infeasible on its
    own. This limit additionally caps how fast anybody can try.

## Exceeding a limit

The response is `429 Too Many Requests` with the body `{"detail": "Rate limit exceeded"}`
and a `Retry-After` header giving the seconds (at least 1) until a request is admitted
again. The UI surfaces it as a toast; API clients should wait that long before retrying.

The window slides: only requests admitted within the last minute count, so there is no
fixed-boundary burst of twice the limit. Rejected requests are not counted, so backing
off is enough to recover. Each route has its own budget per client. If the limiter itself
fails, the request is rejected rather than admitted.

## Tuning

The limits are compiled into the route declarations and are not configurable at runtime —
they encode a security posture rather than a capacity setting. If a limit genuinely
blocks a legitimate workflow, open an issue rather than working around it with a proxy
rule; the numbers reflect what each route costs and what abuse of it would achieve.

## What it does not cover

Rate limiting bounds request frequency, not resource consumption. A single accepted
download can still occupy a worker for an hour. Concurrency and memory are governed
separately — see [Resources & Workers](../configuration/resources.md).
