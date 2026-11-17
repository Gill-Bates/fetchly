<!-- Parent: ../AGENTS.md -->

# app/integrations/ — External Service Clients

**Purpose:** Clients and policy for third-party services. Currently only Lalal.ai, in `lalal/`.

## Key Files

| File | Purpose |
| --- | --- |
| `lalal/client.py` | Lalal.ai API: upload, stem request, polling, result retrieval (`LalalClient`); `LalalUnavailableError` marks a service failure that says nothing about the key (only 401/403 mean an invalid key) |
| `lalal/policy.py` | Policy guards: duration guard, quota and size caps, stem download names (uses `audio.naming`); `lalalaai_duration_guard` (600 s, `LALAL_MAX_DURATION_SECONDS`) applies to trimmed and non-trim splits, and an unknown duration is rejected |

## For AI Agents
- `routes/lalal.py` imports `LalalClient` lazily inside handlers; keep it that way so the client is patchable
- Tests: `tests/test_lalal_minutes.py`, `tests/test_lalal_policy.py`, `tests/test_lalal_wait_for_completion.py`
