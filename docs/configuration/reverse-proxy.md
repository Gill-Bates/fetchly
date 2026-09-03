# Reverse Proxy

fetchly speaks plain HTTP inside the container. Terminate TLS at a proxy and forward to
port `8000`.

## Checklist

| Step | Why |
|---|---|
| Forward to `:8000` | The app's only listener |
| Set `FETCHLY_BEHIND_HTTPS=1` | Marks the session cookie `Secure` |
| Set `FORWARDED_ALLOW_IPS` to the proxy | Makes forwarded scheme and client IP trustworthy |
| Set **Public hostname** in Settings | Share links get a resolvable host |
| Bind the container to loopback | Nobody reaches the app around the proxy |
| Allow SSE to stream | Buffering breaks live job updates |

## Trusted proxies

```yaml
environment:
  FETCHLY_BEHIND_HTTPS: "1"
  FORWARDED_ALLOW_IPS: "172.18.0.0/16"
```

`FORWARDED_ALLOW_IPS` decides whose `X-Forwarded-*` headers are believed. It accepts
comma-separated IPs and CIDRs and defaults to `127.0.0.1,::1`.

!!! danger "`*` is rejected"
    Client IP is what the rate limiter keys on. If any caller's `X-Forwarded-For` were
    trusted, every limit could be bypassed by rotating a header value. fetchly refuses
    to start with a wildcard rather than run in that state — set your proxy's actual
    address or network.

If the value is wrong, every request appears to come from the proxy, so all clients
share one rate-limit bucket and the logs attribute everything to a single address.

## Caddy

An example [`Caddyfile`](https://github.com/Gill-Bates/fetchly/blob/main/Caddyfile)
ships in the repository:

```caddy title="Caddyfile"
example.com {
	encode zstd gzip

	reverse_proxy 127.0.0.1:8000 {
		transport http {
			keepalive 30s
		}
	}

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
		X-Content-Type-Options nosniff
		X-Frame-Options SAMEORIGIN
		Referrer-Policy strict-origin-when-cross-origin

		Content-Security-Policy "
            default-src 'self';
            script-src 'self';
            style-src 'self' 'unsafe-inline';
            img-src 'self' data:;
            connect-src 'self' https:;
            font-src 'self';
            media-src 'self' blob:;
            worker-src blob:;
            object-src 'none';
            base-uri 'self';
            form-action 'self';
            frame-ancestors 'self'
        "

		Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"

		-Server
		-X-Powered-By
	}

	request_body {
		max_size 100MB
	}
}
```

If Caddy runs in Docker on a shared network, use the service name instead:
`reverse_proxy fetchly:8000`.

!!! note "The CSP is deliberately tight"
    `script-src 'self'` works because fetchly vendors Bootstrap and wavesurfer.js
    locally — no CDN. `media-src blob:` and `worker-src blob:` are what the waveform
    needs.

    fetchly sends its own, stricter policy as well, and a browser enforces both. Its
    `style-src` names a hash instead of `'unsafe-inline'`: wavesurfer.js builds one
    stylesheet into the trim view's shadow root, and blocking it breaks the waveform's
    layout. Keep that in mind before replacing the app's header with your own.

## nginx

```nginx
server {
    listen 443 ssl http2;
    server_name fetchly.example.com;

    ssl_certificate     /etc/letsencrypt/live/fetchly.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/fetchly.example.com/privkey.pem;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        # Server-Sent Events: no buffering, no early timeout
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        proxy_http_version 1.1;
    }
}
```

!!! warning "`proxy_buffering off` is not optional"
    With buffering on, nginx holds the SSE stream and the dashboard shows nothing until
    a job finishes. The same applies to any proxy that buffers responses by default.

## Traefik

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.fetchly.rule=Host(`fetchly.example.com`)"
  - "traefik.http.routers.fetchly.entrypoints=websecure"
  - "traefik.http.routers.fetchly.tls.certresolver=letsencrypt"
  - "traefik.http.services.fetchly.loadbalancer.server.port=8000"
```

Set `FORWARDED_ALLOW_IPS` to the Docker network Traefik reaches fetchly on.

## Share links behind a proxy

Share links are built from the `Host` of the request that created them. Behind a proxy
that does not forward `X-Forwarded-Host`, that is an internal name the recipient cannot
resolve.

**Settings → General → Sharing → Public hostname** — enter a bare hostname or IP (no
scheme, port, or path). HTTPS is assumed when it is set. The **Detect** button fills it
from the current request.

See [Share Links](../features/sharing.md).

## Long uploads and downloads

Large files stream through the proxy. Raise the body-size cap and the read timeout, or
big downloads will be cut off mid-transfer:

| Proxy | Setting |
|---|---|
| Caddy | `request_body { max_size 100MB }` |
| nginx | `client_max_body_size`, `proxy_read_timeout` |
| Traefik | `respondingTimeouts.readTimeout` |

## Verifying

```bash
# TLS terminates and the app answers
curl -fsS https://fetchly.example.com/health

# The session cookie is marked Secure
curl -sI https://fetchly.example.com/login | grep -i set-cookie

# SSE streams instead of buffering (expect a keep-alive within ~5 s)
curl -N https://fetchly.example.com/events
```
