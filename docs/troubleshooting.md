# Troubleshooting

## Startup

### The container exits immediately

```bash
docker logs fetchly
```

**`FETCHLY_SECRET_KEY is not set`** — the app refuses to start without it.

```bash
export FETCHLY_SECRET_KEY="$(openssl rand -base64 32)"
```

**`Wildcard trusted proxies are not allowed`** — `FORWARDED_ALLOW_IPS=*` was set. Use
your proxy's address or CIDR instead. See [Reverse Proxy](configuration/reverse-proxy.md).

### The health check never turns healthy

```bash
docker inspect --format='{{json .State.Health}}' fetchly | jq
curl -v http://127.0.0.1:8000/health
```

Check that `DATA_DIR` is writable by the container user (UID 1000 by default) and that
nothing else already holds the port.

## Downloads

### A job fails immediately with a generic error

Open the job — the message field usually names the cause. Common ones:

| Message contains | Meaning | Fix |
|---|---|---|
| "requires authentication" | The platform needs a signed-in session | Import cookies — [Platform Cookies](features/cookies.md) |
| "rate-limit reached" | The platform throttled this connection or session | Wait, or import cookies for a more trusted session |
| "Queue is full" | The job queue is saturated | Raise **Download workers** in Runtime limits, then restart, or wait — [Resources & Workers](configuration/resources.md) |
| "restarted during processing" | The app restarted mid-job | Retry the job |

### Downloads that used to work suddenly fail across the board

Platforms change their internals often; yt-dlp keeps up but needs updating.

```bash
docker exec fetchly yt-dlp --version
```

**Settings → System** shows whether a newer yt-dlp release exists. Pull the latest
image:

```bash
docker compose pull && docker compose up -d
```

### YouTube downloads fail with a JS-challenge or extractor error

YouTube's JS challenges are solved via `yt-dlp-ejs` and `deno`. In the container both
are bundled; in a standalone install, confirm `deno` is on `PATH`:

```bash
deno --version
```

### A download is stuck in `queued`

All worker threads are busy. Check **Settings → General → Runtime limits → Download
workers**, restart after changing it, then check host resources or whether the host is
genuinely out of headroom. See [Resources & Workers](configuration/resources.md).

### `413` or a size-related failure on a large file

Raise **Settings → General → Runtime limits → Maximum input size**.

If behind a reverse proxy, also raise its own body-size limit (`client_max_body_size`
in nginx, `request_body { max_size }` in Caddy).

## Cookies

### The status tile says no session cookie was found

The copied request was not from a page you were signed into, or the wrong request was
copied. Re-do the **Copy as cURL** capture with the Fetch/XHR filter active and confirm
you are logged in in that browser tab. See [Platform Cookies](features/cookies.md).

### Cookies look valid but downloads still say authentication is required

The stored session passed the structural check but the platform has since revoked it —
fetchly never contacts the platform to verify a jar live, by design. Re-import a fresh
capture.

### "Cookie file is too large" / "not UTF-8 text"

Something other than a cookie export was pasted or uploaded — a whole HAR file, a
binary download, or a paste that got mangled. Re-copy just the cookie value or the
*Copy as cURL* command.

## BPM analysis

### A job stays in `analysis` forever, or analysis never completes

Check the BPM analysis track limit in **Settings → Integrations → Lalal.ai** — audio
longer than the limit is skipped, not queued indefinitely. Check the BPM analysis
timeout there too — a run that exceeds it is terminated and the job still finalizes
(without a BPM).

### No BPM is ever detected

- Essentia and/or `beat_this` may not be installed (standalone install only — the
  container bundles both)
- The track may simply have no clear rhythmic pattern the cascade can lock onto
- Check the logs for `beat_this` model-download failures — the first run needs network
  access to `cloud.cp.jku.at` to fetch the ~81 MB checkpoint

```bash
docker exec fetchly ls -la /app/data/.cache/torch
```

If empty and analysis silently falls back to Essentia only, the container likely has
no outbound access to that host.

## Lalal.ai

### The stem action is missing

No activation key is stored. **Settings → Integrations → Lalal.ai**.

### "Selection too long" when splitting

The 10-minute cap is on by default (**Limit long tracks**). [Trim](features/trimming.md)
the section you need first, then split the trim.

### The key was accepted but requests still fail

`GET /api/lalal/status?force_refresh=true` re-checks against Lalal.ai. A key can be
structurally valid but out of credit or revoked on Lalal.ai's side.

## Sharing

### A share link 404s

Every failure — unknown token, expired use limit, or the job's artifacts having been
removed by retention — returns the same page, deliberately. Create a new link from the
job if it still exists.

### The share link has the wrong hostname

Set **Settings → General → Sharing → Public hostname**, or use its **Detect** button.
See [Share Links](features/sharing.md).

## Live updates

### The dashboard does not update in real time

The SSE stream is likely being buffered by a reverse proxy. Confirm:

```bash
curl -N https://fetchly.example.com/events
```

If nothing arrives within ~5 seconds, check `proxy_buffering off;` (nginx) or the
equivalent for your proxy. See [Reverse Proxy](configuration/reverse-proxy.md).

### Updates work for one browser tab but not others

At most 200 concurrent SSE connections and 32 queued events per client are allowed. A
tab that falls behind is disconnected and should reconnect on its own; if not, reload
it.

## Authentication

### Locked out — lost the admin password

There is no reset flow through the UI. With access to the data volume:

```bash
docker compose stop
sqlite3 data/jobs.db "UPDATE settings SET value='false' WHERE key='enable_authentication';"
docker compose start
```

Set new credentials immediately, and only do this while the port is not publicly
reachable.

### Sessions log out sooner than expected

Check **Settings → Security → Session idle timeout** — the window is sliding but still
finite. The 24-hour hard limit is not configurable and applies regardless.

### `403` on a POST from a script

`/login`, `/logout`, and every state-changing route under `/api/*` are CSRF-protected.
Fetch `/login` first to get the `fetchly_csrf` cookie, then echo it back via the
`X-CSRF-Token` header or a `csrf_token` form field. See
[API Authentication](api/authentication.md).

## Performance

### The instance feels slow under load

- Check **Settings → System → Host resources** for CPU/memory pressure
- Confirm `WORKERS=1` (correctness) and tune **Download workers** in Runtime limits
  plus the semaphore limits (throughput) — see [Resources & Workers](configuration/resources.md)
- A saturated queue returns `503` on submit rather than degrading silently; that is
  the system working as designed, not a bug to chase

### High memory usage during analysis

BPM analysis is the heaviest step. Lower `ANALYSIS_SEMAPHORE_LIMIT` to `1` on
memory-constrained hosts, or raise `MEMORY_THRESHOLD_MB` so backpressure engages
earlier.

## Getting more help

1. Check the container logs: `docker logs fetchly` (raise `LOG_LEVEL=debug` for more)
2. Search [existing issues](https://github.com/Gill-Bates/fetchly/issues)
3. Open a new issue with your `docker logs` output, your `docker-compose.yml` (with
   secrets redacted), and the exact steps to reproduce
