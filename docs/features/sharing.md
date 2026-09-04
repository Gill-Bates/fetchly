# Share Links

Hand a finished download to someone who has no account on your instance, without
opening the rest of it up.

## Creating a link

From a completed job, choose **Share**:

```http
POST /api/share/{job_id}
```

```json
{
  "ok": true,
  "url": "https://fetchly.example.com/share/aB3xY7_q",
  "max_uses": 5
}
```

The file is resolved **before** a token is issued, so a job whose artifacts are missing
or not yet downloadable never gets a link handed out for it.

## Token shape

| Property | Value |
|---|---|
| Length | 8 URL-safe characters (48 bits of randomness) |
| Scope | One job's output file |
| Session required to redeem | No |
| Rate limit | 20/minute on both create and redeem |

Short enough to paste into a chat. Brute force is bounded by the rate limit on the
redeem route rather than by token length — which is why that limit is not something to
raise casually.

## Link reuse

Clicking **Share** repeatedly on the same job returns the **same** token, as long as a
still-usable link exists whose snapshotted use limit matches the current setting.
Without that, every click would litter the table and burn quota on links you never
handed out.

Changing the use limit always yields a **fresh** link under the new limit — the old one
keeps the limit it was created with.

## Use limits

**Settings → General → Share link max uses** (`0`–`10000`, default `0` = unlimited)

The value is **snapshotted onto each link at creation time**. Lowering the setting
later never retroactively closes links you already handed out, and raising it never
re-opens exhausted ones. Both directions are deliberate: a link's terms are fixed when
you share it.

A use is counted only after the file is confirmed servable, so a request that 404s
because housekeeping already removed the artifacts does not consume quota.

## Redeeming

```http
GET /share/{token}
```

Anonymous visitors get the file directly. Everything that can go wrong returns the
**same** page with the same `404`:

- unknown token
- malformed token
- use limit exhausted
- artifacts removed by retention or a manual delete

!!! info "Why one error page"
    Naming the actual reason would tell an anonymous visitor whether a given token
    exists. One indistinguishable response for every failure keeps a token from being
    probed for existence.

## Public hostname

Share links are built from the `Host` of the request that created them. Behind a
reverse proxy that is often an internal name (`fetchly:8000`, `10.0.0.5`) that the
recipient cannot resolve.

**Settings → General → Sharing → Public hostname**

Set a bare hostname or IP — no scheme, port, or path. HTTPS is assumed when it is set,
since a public reverse proxy terminates TLS in practically every deployment. Leaving it
empty keeps the request-derived behaviour, including plain HTTP.

There is a **Detect** button that fills it from the current request.

See [Reverse Proxy](../configuration/reverse-proxy.md).

## Revoking

There is no per-link revoke button, and no per-job delete action either. To invalidate
links:

- Let [retention](../configuration/storage.md) sweep the artifacts
- Use **Settings → System → Remove all jobs** — every link dies immediately, but this
  removes every job at once, not just one
- Change the share-link use limit, then create a new link (the old token keeps working
  under its own snapshotted limit until exhausted)

!!! warning "Anyone with the URL has the file"
    A share link is a bearer token. Treat it like a password in a chat window: it is
    forwardable, it may end up in link previews or proxy logs, and it works until its
    use limit runs out or the job is gone.
