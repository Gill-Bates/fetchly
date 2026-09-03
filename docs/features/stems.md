# Stem Separation

fetchly can split a finished audio job into **vocals** and **instrumental** using
[Lalal.ai](https://www.lalal.ai/). The integration is optional and off until you enter
an activation key.

<p align="center">
  <a href="https://www.lalal.ai/"><img src="../../assets/img/lalal_ai.svg" alt="Lalal.ai" width="130"></a>
</p>

## Connecting your account

**Settings → Integrations → Lalal.ai**

1. Enter your Lalal.ai **activation key**
2. Save — the key is validated against Lalal.ai and the status badge updates

| Detail | Value |
|---|---|
| Setting key | `lalalaai_auth_key` (stored as a secret; never returned by the settings API) |
| Validation cache | 5 minutes |
| Rate limit | 5/minute on the activation-key endpoint |

No environment variable is involved. The key lives in the database on your data volume.

!!! danger "The key is a billing credential"
    Anyone with access to your data volume can read it, and anyone who can use your
    fetchly instance can spend your Lalal.ai minutes. Enable
    [authentication](../security/authentication.md) before exposing the instance.

Disconnecting (`POST /api/lalal/auth/logout`) clears the key, the cached validation
state, and the stored email in one write.

## Remaining minutes

The tile shows the processing balance of the connected account under the connect
buttons, next to the account it belongs to: `Logged in as you@example.com - 261m 30s`,
formatted the way Lalal.ai itself reports it.

The number is a by-product of the validation call: `GET /api/lalal/status` validates the
key against `/api/v1/limits/minutes_left/`, which answers with the balance, so no extra
request is made for it. It is cached with the validation state (`lalalaai_minutes_left`,
5 minutes) and refreshed on:

- a finished separation, which is what spends minutes
- `GET /api/lalal/status?force_refresh=1`, which the tile calls after you connect

An account whose balance cannot be determined shows no number at all rather than `0` —
"unknown" and "nothing left" are different answers.

## Splitting a track

From a finished **audio** job, choose the stem action. Under the hood:

```http
POST /api/lalal/{job_id}?stem=vocals
POST /api/lalal/{job_id}?stem=vocals&trimmed=true&trim_id=<start_ms>_<end_ms>
```

| Requirement | Value |
|---|---|
| Job type | Audio only |
| Job status | Downloadable (`done`, `analysis_done`, or `analysis`) |
| Rate limit | 5/minute |

!!! info "One request, two stems"
    Lalal.ai always performs a vocals/instrumental split. The `stem` parameter only
    selects which of the two results is handed back — the other one is written to disk
    at the same time, so asking for it afterwards is free.

Results are cached per source file:

```text
<base_name>_vocals.mp3
<base_name>_instrumental.mp3
```

A repeat request for a stem that already exists returns the cached file without
spending Lalal.ai minutes again. A file lock around the output path prevents two
parallel requests for the same track from both submitting a job.

## Splitting a trimmed section

Passing `trimmed=true` sends a [trim](trimming.md) instead of the full track. This is
the normal workflow for long recordings: trim the section you care about, then split
only that.

## Duration limits

| Limit | Value |
|---|---|
| Maximum track length | 10 minutes |
| "Limit long tracks" toggle | On by default (`lalalaai_duration_guard`) |

With the guard on, tracks over 10 minutes are blocked before a request is sent. Turning
it off lets the request through — Lalal.ai may then reject or partially process it, at
your cost. Trimming first is the reliable path.

## Download size cap

Stem files fetched back from Lalal.ai are capped at **4 GiB** by default. Change the
cap in **Settings → Integrations → Lalal.ai**; it exists so a malformed or hostile
response cannot fill your volume.

## Downloading stems

```http
GET /api/lalal/download/{job_id}?stem=vocals
```

The download name carries the detected tempo when one is known:

```text
Some Track_94bpm.source_vocals.mp3
```

The on-disk name stays plain (`Some Track_vocals.mp3`) because that is what the cache
lookup keys off. See [BPM Analysis](bpm.md).

## When it is unavailable

| Situation | Result |
|---|---|
| No activation key | The stem action is hidden |
| Key invalid or expired | Status badge shows the last error; requests fail |
| Job is a video job | Rejected — audio only |
| Track over 10 min with the guard on | Rejected before any request is sent |
