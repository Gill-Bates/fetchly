<!-- Parent: ../AGENTS.md -->

# app/integrations/ — External Service Clients

**Purpose:** Clients and policy for third-party services. Currently only Lalal.ai, in `lalal/`.

## Key Files

| File | Purpose |
| --- | --- |
| `lalal/client.py` | Lalal.ai API: upload, stem request, polling, result retrieval (`LalalClient`) |
| `lalal/policy.py` | Policy guards: duration guard, quota and size caps, stem download names (uses `audio.naming`) |

## For AI Agents
- `routes/lalal.py` imports `LalalClient` lazily inside handlers; keep it that way so the client is patchable
- Tests: `tests/test_lalal_minutes.py`, `tests/test_lalal_policy.py`, `tests/test_lalal_wait_for_completion.py`
