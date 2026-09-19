<!-- Parent: ../AGENTS.md -->

# docs/configuration/ — Configuration and Deployment

**Purpose:** Detailed configuration documentation: environment variables, reverse proxy setup, persistent storage, resource allocation, and deployment-specific settings.

Pages here guide administrators through configuring fetchly for production: networking, security, storage, and performance tuning.

## Key Files

| File | Purpose |
| --- | --- |
| `environment.md` | Environment variables: complete reference (`FETCHLY_SECRET_KEY`, `DATA_DIR`, `WORKERS`, `FORWARDED_ALLOW_IPS`, `FETCHLY_BEHIND_HTTPS`, `LOG_LEVEL`, …), defaults, when to set. This page is the authoritative env-var reference for the whole repo — other docs should link to it rather than restate it. |
| `reverse-proxy.md` | Reverse proxy setup: Nginx, Caddy, Apache examples; SSL/TLS termination, header forwarding, rate limiting at proxy level |
| `storage.md` | Data storage: database location, media file location, log location, backup strategy, cleanup policies |
| `resources.md` | Resource allocation: CPU/memory requirements, tuning for low-resource systems, monitoring resource usage |
| `settings.md` | Runtime settings: stored in database, accessible via web UI, persistent across restarts, overridable by env vars |

## For AI Agents

### Writing Configuration Docs
- **Audience:** System administrators, DevOps engineers, intermediate to advanced
- **Scope:** How to configure, not how to debug (see troubleshooting); how to deploy (see getting-started)
- **Format:** Reference-style with examples, prerequisites, gotchas
- **Testing:** Test examples on actual systems; verify configurations work end-to-end

### Environment Variable Documentation
For each variable:
- Name (e.g., `FETCHLY_SECRET_KEY`)
- Description (one sentence)
- Required/optional
- Default value (if any)
- Example values
- When to set it
- Impact if not set

Template:
```markdown
### FETCHLY_SECRET_KEY

Signing key for session cookies and the invisible anti-bot token. It signs; it does not encrypt.

- **Required:** Yes
- **Default:** None (the application refuses to start)
- **Example:** `openssl rand -base64 32`
- **When to set:** At first startup
- **Impact if not set:** The app exits at import time — `app/session.py` and `app/main.py` require it
```

### Configuration Examples
Every config example must be:
1. Syntactically correct
2. Tested on actual system
3. Include all required settings
4. Document optional overrides
5. Show expected result

### Common Patterns
- **Secrets:** Never commit passwords/keys; use env vars or file with restricted permissions
- **Defaults:** Document what happens if setting not provided
- **Overrides:** Note which env vars override database settings
- **Backwards compatibility:** Document deprecated settings and migration path

## Dependencies

### Internal
- `/docs/` — Main docs site structure
- `/docs/mkdocs.yml` — Navigation hierarchy (the MkDocs config lives inside `docs/`, with `docs_dir: .`, not at the repo root)
- `../getting-started/` — Basic setup (linked for initial config)
- `../security/` — Security configuration (linked)

### External
- **MkDocs:** Documentation generator
- **Reverse proxy:** Nginx, Caddy, Apache for examples
- **SSL/TLS:** Certificate provider examples

## Manual

### Adding Configuration Option
1. Create environment variable or database setting in code
2. Add documentation section to appropriate file (environment.md, settings.md, etc.)
3. Include required details: description, default, when to set, impact
4. Test configuration on actual system
5. Document example values
6. Add troubleshooting if complex

### Writing Reverse Proxy Example
```markdown
## Nginx Configuration

### Prerequisites
- Nginx installed and running
- fetchly running on localhost:8000
- SSL certificate (from Let's Encrypt or other provider)

### Configuration
```nginx
server {
    listen 443 ssl http2;
    server_name fetchly.example.com;
    
    ssl_certificate /etc/letsencrypt/live/fetchly.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/fetchly.example.com/privkey.pem;
    
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $host;

        # SSE: the job event stream must not be buffered or timed out.
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Setting the headers is only half of it: fetchly ignores them unless the proxy's
address is listed in `FORWARDED_ALLOW_IPS`, and it rejects `*` outright. Document
both sides together, or readers end up with every client rate-limited as one IP.
Behind TLS, also set `FETCHLY_BEHIND_HTTPS=1` so session cookies get `Secure`.

### Testing
1. Reload Nginx: `nginx -s reload`
2. Access https://fetchly.example.com
3. Verify SSL certificate valid
4. Check that fetchly functions (login, submit job, live job updates)
```

### Resource Tuning
Document for different scenarios:
- Low-resource (1 CPU, 2GB RAM)
- Medium (4 CPU, 8GB RAM)
- High-performance (8+ CPU, 16GB+ RAM)

Recommendations to cover:
- Governor semaphores (`CPU_SEMAPHORE_LIMIT`, `ANALYSIS_SEMAPHORE_LIMIT`, `IO_SEMAPHORE_LIMIT`, `TRANSCODE_SEMAPHORE_LIMIT`) and `MEMORY_THRESHOLD_MB`
- Per-job settings in the database: `download_worker_count`, `download_concurrent_fragments` (both `0` = auto-size from the CPU quota)
- Analysis caps: `audio_analysis_max_minutes`, `audio_analysis_timeout_minutes`
- Retention: `retention_days`

**Not** the Gunicorn worker count. `WORKERS` must stay at `1`: the job queue and
the SSE subscriber registry live in process memory with no cross-process
coordination, so a second worker means jobs processed twice and clients
subscribed to a process that never sees their events. Parallelism comes from the
governor's semaphores instead. Any resource-tuning page that suggests raising
`WORKERS` is wrong.

### Backup Strategy
```markdown
## Backing Up Data

### Database Backup
```bash
# Stop fetchly
systemctl stop fetchly

# Backup database — the file is jobs.db, under DATA_DIR
# (./data locally, /app/data in the container)
cp "$DATA_DIR/jobs.db" "backup/jobs-$(date +%Y%m%d).db"

# Restart fetchly
systemctl start fetchly
```

Stopping first matters: SQLite runs in WAL mode, and the clean shutdown
checkpoints the WAL. Copying a live database without it risks an inconsistent
snapshot.

### Media Files Backup
```bash
# Mirror to backup storage
rsync -av "$DATA_DIR/downloads/" /mnt/backup/downloads/
```
```

---

**Last Updated:** 2026-09-19  
**Audience:** System administrators, DevOps engineers  
**Scope:** Configuration, deployment, resource tuning  
**Examples:** Tested on actual systems  
**Platforms:** Docker, Linux, macOS, Windows (WSL2)
