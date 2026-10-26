<!-- Parent: ../AGENTS.md -->

# app/audio/ — Audio Analysis and BPM

**Purpose:** Tempo detection (essentia, then beat-this, fused by confidence), the analysis cache and the BPM tag in download names. The only results are `bpm` and `bpm_confidence`.

## Key Files

| File               | Purpose                                                                                                                                                                                |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `analysis.py`      | Audio analysis orchestration (`extract_analysis`, `AudioAnalysisResult`)                                                                                                               |
| `cache.py`         | Cache lookups and writes for analysis results (`audio_analysis_cache`)                                                                                                                 |
| `hashing.py`       | Content hash used as the cache key                                                                                                                                                     |
| `bpm.py`           | BPM cascade entry point                                                                                                                                                                |
| `beat_this.py`     | beat-this model invocation (imported lazily from `bpm.py`)                                                                                                                             |
| `cluster.py`       | Clustering of detected BPM values                                                                                                                                                      |
| `naming.py`        | Folds a detected tempo into the **download** filename (`Some Track_94bpm.source`); stored filenames are never rewritten                                                                |
| `tags.py`          | ID3 tags of served MP3s: ffmpeg command with cover (JPEG, both edges max 1280 px, never upscaled), Fetchly comment and `TBPM`; `ensure_bpm_tag()` rewrites an existing MP3 in place (stream copy, atomic replace) |
| `normalization.py` | Octave/half-time normalization of raw BPM output                                                                                                                                       |

## For AI Agents
- Only `analysis.py` is run in the spawn child started by `workers/analysis_worker.py`
- `cache.py` imports `db`; everything else here is free of app-internal dependencies
- Tests: `tests/test_bpm_beat_this.py`, `tests/test_bpm_naming.py`, `tests/test_bpm_normalization.py`, `tests/test_analysis_worker.py`, `tests/test_mp3_tags.py`
