<!-- Parent: ../AGENTS.md -->

# docs/api/ — REST API Reference

**Purpose:** Documentation of fetchly's HTTP API: endpoints, request and response shapes, authentication, and error handling.

## Key Files

| File | Purpose |
| --- | --- |
| `overview.md` | API overview: base URL, authentication model, rate limiting, error format |
| `endpoints.md` | Endpoint reference: method, path, parameters, examples |
| `authentication.md` | Session creation via login, CSRF tokens, cookie handling, logout |

## For AI Agents

### Ground rules before documenting anything
The source of truth is `app/routes/`. Read the handler before writing the page. Four properties of this API are easy to get wrong because they differ from common conventions:

1. **There is no response envelope.** Responses are shaped per endpoint. `GET /api/jobs` returns a bare JSON **array**. `GET /api/jobs/{job_id}` returns a bare job object. Some mutations return `{"status": "ok"}`, others `{"ok": true, "message": "..."}`. Do not document a `{data, status, message}` wrapper — nothing returns one.

2. **Errors use FastAPI's default shape:** `{"detail": "..."}`, with the HTTP status carrying the meaning. A rate-limited request returns `429` with `{"detail": "Rate limit exceeded"}`.

3. **Pagination is `offset` + `limit`,** not `page`/`per_page`. On `GET /api/jobs`, `offset` is clamped to `>= 0` and `limit` to `1..100` with a default of `50`.

4. **There are no `X-RateLimit-*` response headers.** Limits are enforced by slowapi per client IP and are visible only as a `429`. Document the limit that the decorator actually declares — for example `@limiter.limit("60/minute")` on the job endpoints and `30/minute` on `/api/stats/bpm-clusters`.

### Job object
`job_to_dict()` in `app/routes/api.py` defines the shape returned by the job endpoints:

```json
{
  "id": "…",
  "url": "https://…",
  "platform": "youtube",
  "video_title": "…",
  "video_meta_hover": "…",
  "type": "audio",
  "quality": "…",
  "status": "done",
  "created_at": "…",
  "finished_at": "…",
  "message": null,
  "filesize_bytes": 0,
  "duration_seconds": 0,
  "codec": "…",
  "bitrate_kbps": 0,
  "bpm": null,
  "bpm_confidence": null,
  "audio_hash": null,
  "filename": "…"
}
```

**Status values** come from `app/db.py`: `queued`, `downloading`, `processing`, `transcoding`, `analysis`, `done`, `analysis_done`, `error`, `cancelled`. The terminal set is `done`, `analysis_done`, `error`, `cancelled`, and `COMPLETED_STATUSES` is `{"done", "analysis_done"}`. There is no `completed` and no `failed` — do not use them in examples.

### Authentication
- Session-based, via cookie. Login is `POST /login` — **not** under `/api/`. Logout is `POST /logout`. Do not confuse it with `POST /api/lalal/auth/logout`, which only clears the Lalal.ai credential.
- Authentication is optional and governed by the `enable_authentication` setting. When it is off, the API is reachable without a session.
- JSON endpoints depend on `require_user`; HTML pages use `require_html_auth`, which redirects rather than returning `401`.
- **CSRF:** state-changing requests under `/api`, `/login` and `/logout` must carry the token in the `X-CSRF-Token` header (or a `csrf_token` form field) matching the CSRF cookie. Document this on every mutating endpoint — a client that omits it gets `403`, not `401`.

### Documenting Endpoints
Consistent structure per endpoint:
- Method and real path, copied from the decorator
- One-sentence description
- Parameters: path, query, body
- Request example (`curl`, including the cookie and `X-CSRF-Token` where required)
- Response example, copied from an actual response — not invented
- Error cases with their status codes
- The declared rate limit, if the handler carries one

### Endpoint Template
```markdown
## GET /api/jobs/{job_id}

Retrieve a single job.

**Auth:** session required when authentication is enabled.
**Rate limit:** 60/minute per client IP.

**Response 200** — the job object (see the job schema above).

**Errors**
- `404` — `{"detail": "Job not found"}`
- `429` — `{"detail": "Rate limit exceeded"}`
```

### Keeping the page honest
When an endpoint changes in `app/routes/`, this page is not updated automatically and nothing in CI checks it. Re-read the handler whenever you touch it. The status vocabulary *is* guarded in three places (`tests/test_status_mapping_parity.py`, `tests/js/config-contract.test.mjs`, `npm run lint:contracts`), so a status rename will fail CI — but a changed response shape will not.

## Dependencies

### Internal
- `/app/routes/` — the handlers this documents, and the only authority for paths and shapes
- `/app/db.py` — the job status vocabulary
- `/middleware/csrf.py` — the CSRF requirement documented here
- `/docs/mkdocs.yml` — navigation hierarchy (the MkDocs config lives inside `docs/`, with `docs_dir: .`, not at the repo root)

### External
- **MkDocs Material:** rendering, admonitions, content tabs
- **pymdown-extensions:** fenced code blocks and tabbed examples

## Manual

### Previewing
```bash
mkdocs serve -f docs/mkdocs.yml
```

### Verifying an Example Against the Running App
```bash
# Log in and keep the cookie jar
curl -c jar.txt -X POST http://127.0.0.1:8000/login \
  -d "username=admin&password=…"

# Read the CSRF cookie, then call a mutating endpoint
curl -b jar.txt -H "X-CSRF-Token: $TOKEN" \
  -X POST http://127.0.0.1:8000/api/submit \
  -H "Content-Type: application/json" \
  -d '{"url": "https://…"}'
```

Paste the real response into the page rather than a plausible one.

---

**Last Updated:** 2026-09-19  
**Response Format:** Per-endpoint shapes; no envelope  
**Errors:** `{"detail": "..."}` with a meaningful HTTP status  
**Pagination:** `offset` + `limit` (limit 1–100, default 50)
