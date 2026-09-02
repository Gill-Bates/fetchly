# Best Practices

A deployment checklist, ordered by how much it matters.

## Essential

### 1. Turn authentication on before exposing anything

!!! danger "This is the one that bites"
    A fresh instance has no account and no login. Anyone who reaches the port has full
    access — every download, every cookie jar, your Lalal.ai minutes.

```bash
# Bind to loopback until the account exists
ports:
  - "127.0.0.1:8000:8000"
```

Then create the admin account under **Settings → Security**.

### 2. Generate a real secret key

```bash
openssl rand -base64 32
```

Never reuse a key across instances, never commit it, never bake it into an image.

!!! warning "The key is effectively permanent"
    The password salt and pepper are derived from it. Rotating it invalidates the
    stored password and every session and anti-bot token. Decide once, store it
    durably.

### 3. Terminate TLS

Put a reverse proxy in front and set:

```yaml
environment:
  FETCHLY_BEHIND_HTTPS: "1"
  FORWARDED_ALLOW_IPS: "172.18.0.0/16"   # your proxy's network
```

Without `FETCHLY_BEHIND_HTTPS=1` the session cookie is not marked `Secure`. Without a
correct `FORWARDED_ALLOW_IPS` every client shares one rate-limit bucket. See
[Reverse Proxy](../configuration/reverse-proxy.md).

### 4. Protect the data volume

```bash
chmod 700 data/
```

It holds live platform sessions, your Lalal.ai key, and the admin password hash. Back
it up encrypted.

## Strongly recommended

### 5. Do not put it on the public internet

fetchly is a single-user tool. The safest deployment is on a private network or behind
a VPN. If it must be reachable from outside, at minimum:

- Authentication on
- TLS with a valid certificate
- Restrictive proxy access rules (IP allow-list, or an auth layer in front)

### 6. Keep `WORKERS` at 1

Not a performance knob. More than one process means duplicate jobs and missing live
updates. Scale with **Settings → General → Runtime limits → Download workers** instead.

### 7. Set a retention period

```text
Settings → General → Retention: 30 days
```

Unlimited retention means downloaded media accumulates until the disk fills. It also
means a share link handed out a year ago still works.

### 8. Cap share links

```text
Settings → General → Share link max uses: 5
```

Unlimited links are bearer tokens with no expiry. A modest use count limits the damage
when one is forwarded further than you intended.

### 9. Set the public hostname

```text
Settings → General → Sharing → Public hostname: fetchly.example.com
```

Otherwise share links may carry an internal hostname and leak your topology while not
working for the recipient.

## Recommended

### 10. Harden the container

```yaml
security_opt:
  - no-new-privileges:true
read_only: true
tmpfs:
  - /tmp
```

`read_only` needs the `/tmp` tmpfs — ffmpeg and the cookie importer write there.

### 11. Bound the logs

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "5"
```

### 12. Keep it updated

**Settings → System** shows the running version against the latest release, plus the
component versions of yt-dlp, ffmpeg, and friends. yt-dlp in particular breaks when
platforms change; a stale image shows up as downloads that used to work and no longer
do.

```bash
docker compose pull && docker compose up -d
```

### 13. Test your backups

```bash
docker compose stop
tar czf fetchly-backup-$(date +%F).tar.gz data/
docker compose start
```

A backup you have never restored is a hypothesis.

## Cookie hygiene

- Import a session only for platforms you actually need gated content from
- Prefer a secondary account over your primary one where the platform allows it
- Signing out in the browser you copied from invalidates the imported jar too
- Remove a platform's jar when you stop needing it
- Never share a snapshot of the data volume — it contains working sessions

## Anti-checklist

Things that look like hardening and are not:

| Idea | Why it does not help |
|---|---|
| `FORWARDED_ALLOW_IPS=*` "to make the proxy work" | Disables client-IP integrity; every rate limit becomes bypassable |
| Raising the share-redeem rate limit | That limit is what makes 48-bit tokens safe |
| Running more Gunicorn workers "for safety" | Breaks job processing and live updates |
| Relying on an unguessable port | Port scans are cheap; this is not access control |
| Turning off the Lalal.ai duration guard to "save a step" | Sends oversized jobs at your own cost |

## Verifying a deployment

```bash
# App answers
curl -fsS https://fetchly.example.com/health

# Authentication is on: an unauthenticated API call is refused
curl -si https://fetchly.example.com/api/jobs | head -1

# The session cookie carries Secure and HttpOnly
curl -sI https://fetchly.example.com/login | grep -i set-cookie

# The port is not reachable around the proxy
curl -m 3 http://<host>:8000/health   # should fail
```
