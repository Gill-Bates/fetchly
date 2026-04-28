#!/usr/bin/env python3
#
# app/bpm.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# BPM (beats per minute) detection for audio files using Essentia.
# Callers are responsible for concurrency control.
#

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Final, NamedTuple

logger = logging.getLogger(__name__)

# Only analyze first 2 minutes for long files (performance optimization)
_MAX_ANALYSIS_DURATION: Final[int] = 120
_DEFAULT_WINDOW_STARTS: Final[tuple[int, ...]] = (0, 60, 120)
_MIN_SEGMENT_BYTES: Final[int] = (44_100 * 2) + 44
_FFMPEG_ERROR_TAIL_BYTES: Final[int] = 500
_FFMPEG_TIMEOUT_SECONDS: Final[int] = 120

# BPM normalization range (most music falls within 70-180 BPM)
_BPM_MIN: Final[float] = 70.0
_BPM_MAX: Final[float] = 180.0

# Minimum confidence threshold for valid BPM
_MIN_CONFIDENCE: Final[float] = 0.1

_essentia_local = threading.local()


class BPMResult(NamedTuple):
    """Result of BPM detection."""
    bpm: float
    confidence: float


def _ensure_file_exists(file_path: Path) -> None:
    if not file_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {file_path}")


def _get_rhythm_extractor():
    """Return a thread-local Essentia rhythm extractor instance."""
    extractor = getattr(_essentia_local, "rhythm_extractor", None)
    if extractor is None:
        from essentia.standard import RhythmExtractor2013

        extractor = RhythmExtractor2013(method="multifeature")
        _essentia_local.rhythm_extractor = extractor
    return extractor


def _run_ffmpeg(
    input_path: Path,
    output_path: Path,
    *,
    filters: str | None = None,
    duration: int | None = None,
    start_offset: int | None = None,
) -> None:
    """
    Convert audio to mono 44.1kHz WAV for analysis.
    
    Args:
        input_path: Source audio file
        output_path: Destination WAV file
        filters: Optional audio filters (e.g., "highpass=f=40,loudnorm")
        duration: Optional max duration in seconds to extract
        start_offset: Optional seek offset in seconds for segment extraction
    """
    cmd = ["ffmpeg", "-y"]

    if start_offset is not None:
        cmd.extend(["-ss", str(start_offset)])

    cmd.extend([
        "-i", str(input_path),
        "-ac", "1",          # Mono
        "-ar", "44100",      # 44.1kHz sample rate
        "-vn",               # No video
    ])

    if duration is not None:
        cmd.extend(["-t", str(duration)])

    if filters:
        cmd.extend(["-af", filters])

    cmd.append(str(output_path))

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        stderr_text = (exc.stderr or b"").decode(errors="replace")
        tail = stderr_text[-_FFMPEG_ERROR_TAIL_BYTES:].strip()
        raise RuntimeError(
            f"ffmpeg failed for {input_path.name} (exit {exc.returncode}): {tail or 'no stderr output'}"
        ) from exc


def _normalize_bpm(bpm: float) -> float:
    """
    Normalize BPM to standard range (70-180).
    
    Many BPM detectors return half or double the actual tempo.
    This heuristic normalizes to the most common musical range.
    """
    if not math.isfinite(bpm) or bpm <= 0:
        return 0.0

    if bpm < _BPM_MIN:
        bpm *= 2 ** math.ceil(math.log2(_BPM_MIN / bpm))
    elif bpm > _BPM_MAX:
        bpm /= 2 ** math.ceil(math.log2(bpm / _BPM_MAX))

    if bpm > _BPM_MAX:
        bpm /= 2

    return bpm


def _extract_bpm_essentia(audio_path: Path) -> BPMResult:
    """
    Extract BPM using Essentia's RhythmExtractor2013.
    
    Args:
        audio_path: Path to mono WAV file
        
    Returns:
        BPMResult with detected BPM and confidence score
    """
    # Late import to avoid loading heavy library at module level
    from essentia.standard import MonoLoader

    loader = MonoLoader(filename=str(audio_path))
    audio = loader()

    bpm, _, confidence, _, _ = _get_rhythm_extractor()(audio)

    bpm = float(_normalize_bpm(bpm))
    confidence = float(confidence)

    return BPMResult(bpm=bpm, confidence=confidence)


def extract_bpm(
    file_path: Path,
    *,
    max_duration: int = _MAX_ANALYSIS_DURATION,
) -> BPMResult:
    """
    Extract BPM from an audio file.
    
    This function:
    1. Decodes the audio to WAV (handles any format ffmpeg supports)
    2. Applies preprocessing (highpass filter, normalization)
    3. Runs Essentia BPM detection
    4. Normalizes the result to standard range
    
    Args:
        file_path: Path to audio file (MP3, WAV, FLAC, etc.)
        max_duration: Maximum seconds to analyze (default 120s for performance)
        
    Returns:
        BPMResult with detected BPM (70-180 range) and confidence (0-1).
        Returns bpm=0.0 when detection is too unreliable to trust.
        
    Raises:
        FileNotFoundError: If the input file doesn't exist
        RuntimeError: If BPM detection fails
    """
    _ensure_file_exists(file_path)

    with tempfile.TemporaryDirectory(prefix="bpm_") as tmp:
        tmp_dir = Path(tmp)
        clean_wav = tmp_dir / "clean.wav"

        # Decode to mono WAV, apply preprocessing, and limit duration in one pass.
        # - highpass=f=40: Remove sub-bass rumble that confuses beat detection
        # - loudnorm: Normalize loudness for consistent detection
        logger.debug("Decoding and preprocessing %s for BPM analysis", file_path.name)
        _run_ffmpeg(
            file_path,
            clean_wav,
            duration=max_duration,
            filters="highpass=f=40,loudnorm",
        )

        # Run Essentia BPM detection
        logger.debug("Running Essentia BPM detection")
        result = _extract_bpm_essentia(clean_wav)

        if result.confidence < _MIN_CONFIDENCE:
            logger.debug(
                "Ignoring low-confidence BPM result for %s (confidence=%.2f)",
                file_path.name,
                result.confidence,
            )
            result = BPMResult(bpm=0.0, confidence=result.confidence)

    logger.debug(
        "BPM detection complete: %.1f BPM (confidence: %.2f)",
        result.bpm,
        result.confidence,
    )

    return result


def extract_bpm_multi_window(
    file_path: Path,
    *,
    window_starts: tuple[int, ...] = _DEFAULT_WINDOW_STARTS,
    window_duration: int = 30,
) -> BPMResult:
    """
    Extract BPM using multiple analysis windows for improved stability.
    
    Analyzes multiple segments and returns the median BPM.
    Useful for tracks with tempo variations or intro sections.
    
    Args:
        file_path: Path to audio file
        window_starts: Start times in seconds for each analysis window
        window_duration: Duration of each window in seconds
        
    Returns:
        BPMResult with median BPM and average confidence
    """
    # Late import to avoid loading at module level
    import numpy as np
    from essentia.standard import MonoLoader

    _ensure_file_exists(file_path)

    bpms: list[float] = []
    confidences: list[float] = []

    with tempfile.TemporaryDirectory(prefix="bpm_") as tmp:
        tmp_dir = Path(tmp)

        for i, start in enumerate(window_starts):
            segment_wav = tmp_dir / f"segment_{i}.wav"

            try:
                _run_ffmpeg(
                    file_path,
                    segment_wav,
                    filters="highpass=f=40,loudnorm",
                    duration=window_duration,
                    start_offset=start,
                )

                if not segment_wav.is_file() or segment_wav.stat().st_size < _MIN_SEGMENT_BYTES:
                    logger.debug("Segment %d too short for BPM analysis, skipping", i)
                    continue

                loader = MonoLoader(filename=str(segment_wav))
                audio = loader()

                bpm, _, confidence, _, _ = _get_rhythm_extractor()(audio)

                bpm = float(_normalize_bpm(bpm))
                if float(confidence) >= _MIN_CONFIDENCE and bpm > 0:
                    bpms.append(bpm)
                    confidences.append(float(confidence))

            except (RuntimeError, OSError, ValueError) as exc:
                logger.warning("Segment %d analysis failed: %s", i, exc)
                continue

    if not bpms:
        # Fall back to single-window analysis
        return extract_bpm(file_path)

    median_bpm = float(np.median(bpms))
    avg_confidence = float(np.mean(confidences))

    logger.debug(
        "Multi-window BPM: %.1f (windows=%d, confidence=%.2f)",
        median_bpm,
        len(bpms),
        avg_confidence,
    )

    return BPMResult(bpm=median_bpm, confidence=avg_confidence)
