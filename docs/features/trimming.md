# Audio Trimming

Cut a segment out of a downloaded audio track visually, without re-downloading it and
without touching `ffmpeg` yourself.

## How it works

The job page renders the downloaded audio as an interactive waveform using
[wavesurfer.js](https://wavesurfer.xyz/). Drag a region, adjust the handles, preview
it, and export.

| Gesture | Effect |
|---|---|
| Drag | Create or move the selection |
| Scroll | Zoom |
| Drag (outside a region) | Pan |
| Double-click / double-tap | Reset the zoom |

Server-side, `POST /api/trim/{job_id}` re-encodes the selected range with ffmpeg and
writes a WAV file next to the source.

## Constraints

| Rule | Value |
|---|---|
| Job type | Audio only |
| Job status | Must be downloadable (`done`, `analysis_done`, or `analysis`) |
| Minimum selection | 1 second |
| Maximum selection | 10 minutes |
| Rate limit | 10/minute |

The maximum matches the Lalal.ai per-request duration cap, so any trim you produce can
be fed straight into [stem separation](stems.md).

## Deterministic trim IDs

Start and end are quantized to milliseconds and used verbatim as the trim identifier:

```text
trim_<start_ms>_<end_ms>.wav
```

The same selection therefore always resolves to the same file. Asking for a range you
already trimmed returns the existing output instead of re-encoding it, and a file lock
around the output path keeps two parallel requests for the same segment from racing
each other.

## Why WAV

The trim output is PCM WAV, not a stream copy of the source:

- **Accuracy** — the seek is placed after the input so the cut lands where you put it,
  which requires a re-encode
- **Compatibility** — WAV is what Lalal.ai accepts most reliably

This is lossless relative to the decoded source, but it is not bit-exact with the
original encoded stream.

## Downloading a trim

```http
GET /api/trim/{job_id}/{trim_id}/download
```

The filename handed to the browser carries the detected tempo when one is known —
`Some Track_94bpm...` — while the file on disk keeps its plain name. See
[BPM Analysis](bpm.md).

## Deleting a trim

```http
DELETE /api/trim/{job_id}
```

Removes the trim outputs for that job. The source download is untouched.

## Trims and stems

A trim can be sent to Lalal.ai directly:

```http
POST /api/lalal/{job_id}?trimmed=true&trim_id=<start_ms>_<end_ms>
```

This is the usual workflow for long recordings: trim the section you care about, stay
under the 10-minute cap, and only spend Lalal.ai minutes on that section.
