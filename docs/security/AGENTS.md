<!-- Parent: ../AGENTS.md -->

# docs/security/ — Security Configuration and Best Practices

**Purpose:** Security-focused documentation: CSRF protection, rate limiting, authentication, CAPTCHA setup, HTTPS/TLS, and firewall rules.

Pages here guide administrators through securing fetchly deployments against common attacks and protecting user data.

## Key Files

| File | Purpose |
| --- | --- |
| `overview.md` | Security overview: threat model, built-in protections, assumptions (single-user app by default), security by design |
| `authentication.md` | Authentication security: password requirements, brute-force protection, session management, timeout settings, CAPTCHA configuration |
| `best-practices.md` | Deployment best practices: HTTPS/TLS enforcement, firewall rules, network isolation, reverse proxy hardening, monitoring |
| `rate-limiting.md` | Rate limiting: per-endpoint slowapi limits, trusted proxies via `FORWARDED_ALLOW_IPS`, client-IP resolution behind a reverse proxy, DDoS mitigation at the proxy layer. Counters are in-process memory only — there is no Redis or other shared backend. |
| `anti-bot.md` | Anti-bot protection: CAPTCHA setup (honeypot + token), bot scoring, false-positive handling |

## For AI Agents

### Writing Security Documentation
- **Audience:** System administrators, security engineers, intermediate to advanced
- **Scope:** Security configuration and hardening; threat model and design rationale
- **Format:** Clear guidance with examples, trade-offs, monitoring/validation
- **Testing:** Test security measures (attempt attacks, verify protection)

### Security Checklist Template
```markdown
## Security Checklist

Before deploying fetchly to production:

- [ ] FETCHLY_SECRET_KEY set to cryptographically random value (min 32 bytes)
- [ ] HTTPS/TLS enabled on reverse proxy
- [ ] `public_hostname` setting set to the actual domain when behind a reverse proxy (Settings UI; empty means "use the host of the request that created the link")
- [ ] `FETCHLY_BEHIND_HTTPS=1` so session cookies are marked `Secure`
- [ ] `FORWARDED_ALLOW_IPS` restricted to the reverse proxy (never `*`; the app rejects wildcards)
- [ ] Database file permissions set to 0600 (owner read/write only)
- [ ] Regular backups enabled and tested
- [ ] Firewall rules configured (see Firewall Rules below)
- [ ] Rate limiting enabled (see Rate Limiting below)
- [ ] `enable_authentication` turned on if the instance is reachable beyond a trusted network (it is **off** by default)
- [ ] Admin account created with a strong password (there are no default credentials to change — `admin_username` starts empty)
- [ ] Logs monitored for suspicious activity
- [ ] Security updates applied promptly
```

### CSRF Protection Documentation
```markdown
## CSRF Protection

fetchly uses double-submit CSRF tokens on all state-changing operations.

### How It Works
1. A request to a protected prefix (`/login`, `/logout`, `/api`) without a CSRF
   cookie gets one issued — this happens on any such request, not only at login
2. The frontend reads the cookie and sends the value back as `X-CSRF-Token`
   (forms use a hidden `csrf_token` field instead)
3. The server compares header/field against cookie before processing

### Requirements
- Browser cookies enabled
- `SameSite=Lax` on the CSRF cookie, no `Max-Age` (a browser-session cookie, so it
  cannot expire out from under a login left open across the full `session_max_days`)
- `Secure` over HTTPS in production (set `FETCHLY_BEHIND_HTTPS=1`)

### Why the cookie is not HttpOnly
Double-submit requires JavaScript to read the token, so the CSRF cookie is
deliberately set with `httponly=False`. Do not document it as HttpOnly, and do
not recommend "hardening" it — that breaks every API call. The protection comes
from `SameSite` plus the origin check, not from hiding the cookie.
```

### Rate Limiting Guidance
- Explain purpose (brute-force protection, DDoS mitigation)
- Document limits per endpoint
- Explain IP detection (X-Forwarded-For with trusted proxy validation)
- Note false positives (shared IP scenarios)
- Provide tuning guidance

### Common Vulnerabilities to Address
- **SQL Injection:** `app/db.py` writes raw SQL by hand, always parameterized. Status values are additionally validated against a fixed set before they reach a query.
- **XSS:** HTML sanitization via nh3 (changelog rendering); Jinja2 auto-escaping
- **CSRF:** double-submit tokens on the `/login`, `/logout` and `/api` prefixes
- **Brute force:** slowapi rate limiting (`5/minute` on the login POST, `20/minute` on the surrounding auth routes) plus the invisible anti-bot check
- **Untrusted input:** submitted URLs reach yt-dlp as subprocess arguments — validation before that boundary is the relevant control
- **Session handling:** stateless HMAC-SHA256 tokens with an absolute lifetime (`session_max_days`, 1–7 days); `Secure` when `FETCHLY_BEHIND_HTTPS=1`

## Dependencies

### Internal
- `/docs/` — Main docs site structure
- `/docs/mkdocs.yml` — Navigation hierarchy (the MkDocs config lives inside `docs/`, with `docs_dir: .`, not at the repo root)
- `../configuration/` — Configuration (linked for specific settings)
- `../getting-started/` — Initial setup (linked for prerequisites)

### External
- **MkDocs:** Documentation generator
- **Security tools:** Examples may reference external tools (nmap, curl, etc.)

## Manual

### Security Audit Checklist
```bash
# Verify HTTPS
curl -I https://fetchly.example.com
# Should show 200 OK, no redirect to HTTP

# Check HSTS header (if implemented)
curl -I https://fetchly.example.com | grep -i "Strict-Transport-Security"

# Test CSRF protection
# Try POST without token - should fail with 403

# Check rate limiting — the login route is /login, not /api/login
for i in {1..20}; do curl -X POST https://fetchly.example.com/login; done
# After the limit is exceeded: 429 with {"detail": "Rate limit exceeded"}
```

### Testing the Anti-Bot Check
`app/utils/hidden_captcha.py` is an **invisible, no-interaction** check on the
login form — not a visible challenge. There is nothing for a human to solve, and
it is not triggered after N failed attempts; it is present on every login form.

Two signals must both pass:
1. A CSS-hidden honeypot input must be left empty. A form-filler that populates
   every field trips `HONEYPOT_FILLED`.
2. A signed, timestamped token minted by `issue_captcha_token()` must come back
   present, correctly signed and unexpired.

To test it: submit the login form with the honeypot field filled (expect
rejection), and submit without the token (expect rejection). Do not document
steps that ask the user to "solve" anything.

### Password Policy
```markdown
## Password Requirements

The enforced policy is length only, from `PASSWORD_MIN_LENGTH` and
`PASSWORD_MAX_LENGTH` in `app/utils/credentials.py`:

- Minimum 8 characters
- Maximum 1024 characters

There are no composition rules — no uppercase, digit, dictionary or
repeated-pattern checks — and no `FETCHLY_PASSWORD_POLICY` environment variable.
The constants are the only knob, and changing them is a code change. Document
the real policy and recommend a passphrase; do not describe rules the app does
not enforce, or operators will believe they are protected by checks that do not run.
```

### Monitoring for Security Issues
```markdown
## Log Monitoring

Watch for suspicious patterns:
- Repeated 401 responses (failed login attempts)
- Repeated 429 responses (rate limit exceeded)
- POST requests with invalid CSRF tokens
- Unusual geographic locations

Set up alerts. fetchly logs to **stdout/stderr** — there is no `app/logs/`
directory and no log file — so monitoring goes through whatever collects the
process output:

```bash
# Container
docker compose logs -f fetchly | grep -E '" (401|403|429) '

# systemd
journalctl -u fetchly -f | grep -E '" (401|403|429) '
```

The access-log format is set by `ACCESS_LOG_FORMAT` in the container, so adjust
the pattern if it has been customized.
```

### Security Response Process
1. Report security issue privately (don't disclose publicly)
2. Reproduce issue locally
3. Develop fix and test
4. Release patch
5. Notify users to upgrade

---

**Last Updated:** 2026-09-19  
**Audience:** System administrators, security engineers  
**Scope:** Security configuration, threat model, best practices  
**Threat Model:** Single-user app by default; assumes trusted network for bare-metal deployments  
**Testing:** Security measures verified with actual attack attempts
