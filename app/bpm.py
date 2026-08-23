#!/usr/bin/env python3
#
# app/bpm.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# BPM (beats per minute) detection for audio files using Essentia.
# Callers are responsible for concurrency control.
#

import logging
import math
from statistics import fmean, median
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Final, NamedTuple

logger = logging.getLogger(__name__)

# Only analyze first 2 minutes for long files (performance optimization)
_MAX_ANALYSIS_DURATION: Final[int] = 120
type WindowStarts = tuple[int, ...]

_DEFAULT_WINDOW_STARTS: Final[WindowStarts] = (0, 60, 120)
_WAV_HEADER_BYTES: Final[int] = 44
_MONO_16BIT_44K_BYTES_PER_SECOND: Final[int] = 44_100 * 2
# Require roughly one second of mono 16-bit 44.1kHz WAV audio plus the header.
_MIN_SEGMENT_BYTES: Final[int] = _MONO_16BIT_44K_BYTES_PER_SECOND + _WAV_HEADER_BYTES
_FFMPEG_ERROR_TAIL_BYTES: Final[int] = 500
_FFMPEG_TIMEOUT_SECONDS: Final[int] = 120

# BPM normalization range (most music falls within 70-180 BPM)
_BPM_MIN: Final[float] = 70.0
_BPM_MAX: Final[float] = 180.0

# Minimum confidence threshold for valid BPM
_MIN_CONFIDENCE: Final[float] = 0.2

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
    """Run ffmpeg with optional audio filters, seek offset, and duration cap.

    Args:
        input_path: Source audio file in any ffmpeg-supported format.
        output_path: Destination file; format is inferred from its extension.
        filters: Optional ffmpeg audio filter graph passed via ``-af``.
        duration: Optional maximum output duration in seconds passed via ``-t``.
        start_offset: Optional seek offset in seconds passed via ``-ss``.

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero status or exceeds the timeout.
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

    with tempfile.SpooledTemporaryFile(max_size=_FFMPEG_ERROR_TAIL_BYTES) as stderr_file:
        def _stderr_tail() -> str:
            stderr_file.seek(0, 2)
            size = stderr_file.tell()
            stderr_file.seek(max(0, size - _FFMPEG_ERROR_TAIL_BYTES))
            return stderr_file.read().decode(errors="replace").strip()

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                timeout=_FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"ffmpeg failed for {input_path.name} (exit {exc.returncode}): {_stderr_tail() or 'no stderr output'}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ffmpeg timed out for {input_path.name}: {_stderr_tail() or 'no stderr output'}"
            ) from exc


def _normalize_bpm(bpm: float) -> float:
    """Normalize BPM into the 70-180 range via powers-of-two scaling.

    BPM detectors commonly return binary multiples of the perceived tempo
    (for example x0.5, x2, or x4). This helper doubles or halves by powers of
    two until the value fits the expected musical range. A final halve corrects
    the rare case where rounding up the scaling factor overshoots the upper
    bound.

    Returns:
        Normalized BPM in [70, 180], or 0.0 when the input is non-finite or <= 0.
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
    # Audio already resampled via ffmpeg, keep loader simple
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
    """Extract BPM from an audio file using a single analysis pass.

    Steps:
    1. Decode to mono 44.1 kHz WAV via ffmpeg.
    2. Apply highpass filtering and loudness normalization.
    3. Run Essentia RhythmExtractor2013 BPM detection.
    4. Normalize the detected BPM into the 70-180 range.
    5. Return ``bpm=0.0`` when the confidence is below ``_MIN_CONFIDENCE``.

    Args:
        file_path: Path to audio file (MP3, WAV, FLAC, etc.)
        max_duration: Maximum seconds to analyze (default 120s for performance)

    Returns:
        BPMResult with detected BPM and confidence. BPM is forced to 0.0 when
        the detector result is too unreliable to trust.

    Raises:
        FileNotFoundError: If the input file doesn't exist.
        RuntimeError: If ffmpeg or BPM detection fails.
    """
    _ensure_file_exists(file_path)

    with tempfile.TemporaryDirectory(prefix="bpm_") as tmp:
        tmp_dir = Path(tmp)
        clean_wav = tmp_dir / "clean.wav"

        # Decode to mono WAV, apply preprocessing, and limit duration in one pass.
        # - highpass=f=40: Remove sub-bass rumble that confuses beat detection
        # Avoid loudnorm: it can distort transients and harm beat detection
        logger.debug("Decoding and preprocessing %s for BPM analysis", file_path.name)
        _run_ffmpeg(
            file_path,
            clean_wav,
            duration=max_duration,
            filters="highpass=f=40",
        )

        # Run Essentia BPM detection
        logger.debug("Running Essentia BPM detection")
        result = _extract_bpm_essentia(clean_wav)

        # Reject clearly implausible BPM values (noise/speech edge cases)
        if result.bpm < 60 or result.bpm > 200:
            logger.debug(
                "Rejecting implausible BPM %.1f for %s",
                result.bpm,
                file_path.name,
            )
            result = BPMResult(bpm=0.0, confidence=result.confidence)

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
    window_starts: WindowStarts = _DEFAULT_WINDOW_STARTS,
    window_duration: int = 30,
) -> BPMResult:
    """Extract BPM from multiple time windows for improved stability.

    Each requested window is analyzed independently and successful detections are
    merged using the median BPM plus mean confidence. If every window fails or
    produces only low-confidence results, this falls back to ``extract_bpm()``.

    Args:
        file_path: Path to audio file.
        window_starts: Start times in seconds for each analysis window.
        window_duration: Duration of each window in seconds.

    Returns:
        BPMResult with the median BPM and mean confidence across successful
        windows, or the fallback result from ``extract_bpm()`` when no window
        succeeds.
    """

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
                    filters="highpass=f=40",
                    duration=window_duration,
                    start_offset=start,
                )

                if not segment_wav.is_file() or segment_wav.stat().st_size < _MIN_SEGMENT_BYTES:
                    logger.debug("Segment %d too short for BPM analysis, skipping", i)
                    continue

                result = _extract_bpm_essentia(segment_wav)

                # Reject implausible BPM values
                if result.bpm < 60 or result.bpm > 200:
                    continue

                if result.confidence >= _MIN_CONFIDENCE and result.bpm > 0:
                    bpms.append(result.bpm)
                    confidences.append(result.confidence)

            except (RuntimeError, OSError, ValueError) as exc:
                logger.warning("Segment %d analysis failed: %s", i, exc)
            finally:
                segment_wav.unlink(missing_ok=True)

    if not bpms:
        # Fall back to single-window analysis
        return extract_bpm(file_path)

    median_bpm = float(median(bpms))
    avg_confidence = float(fmean(confidences))

    logger.debug(
        "Multi-window BPM: %.1f (windows=%d, confidence=%.2f)",
        median_bpm,
        len(bpms),
        avg_confidence,
    )

    return BPMResult(bpm=median_bpm, confidence=avg_confidence)


def extract_bpm_cascade(
    file_path: Path,
    *,
    max_duration: int = _MAX_ANALYSIS_DURATION,
) -> BPMResult:
    """Extract BPM using a cascade of algorithms for maximum accuracy.

    The cascade runs:
    1. Essentia RhythmExtractor2013 (fast, good baseline)
    2. beat_this with optional DBN postprocessing (state-of-the-art accuracy)

    Results are combined using confidence-weighted averaging. If both algorithms
    agree (within 5 BPM), confidence is boosted. If they disagree significantly,
    the higher-confidence result is preferred.

    Args:
        file_path: Path to audio file (MP3, WAV, FLAC, etc.)
        max_duration: Maximum seconds to analyze (default 120s for performance)

    Returns:
        BPMResult with the best BPM estimate and combined confidence.
    """
    _ensure_file_exists(file_path)

    # Step 1: Run Essentia.
    logger.debug("Cascade step 1: Running Essentia BPM detection for %s", file_path.name)
    try:
        essentia_result = extract_bpm(file_path, max_duration=max_duration)
    except MemoryError:
        raise
    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        logger.warning("Essentia BPM detection failed: %s", exc)
        essentia_result = BPMResult(bpm=0.0, confidence=0.0)

    # Step 2: Run beat_this with optional DBN postprocessing
    logger.debug("Cascade step 2: Running beat_this for %s", file_path.name)
    try:
        from .bpm_beat_this import extract_bpm_beat_this, is_beat_this_available

        if not is_beat_this_available():
            logger.debug("beat_this not available, using Essentia result only")
            return essentia_result

        with tempfile.TemporaryDirectory(prefix="beat_this_") as tmp:
            bounded_audio = Path(tmp) / "input.wav"
            _run_ffmpeg(file_path, bounded_audio, duration=max_duration)
            beat_this_result = extract_bpm_beat_this(bounded_audio)
    except MemoryError:
        raise
    except ImportError:
        logger.debug("beat_this import failed, using Essentia result only")
        return essentia_result
    except (RuntimeError, OSError, ValueError) as exc:
        logger.warning("beat_this failed: %s, using Essentia result only", exc)
        return essentia_result

    # Step 3: Combine results
    essentia_bpm = essentia_result.bpm
    essentia_conf = essentia_result.confidence
    beat_this_bpm = beat_this_result.bpm
    beat_this_conf = beat_this_result.confidence

    # If one algorithm failed, use the other
    if essentia_bpm <= 0 and beat_this_bpm <= 0:
        logger.debug("Both algorithms failed for %s", file_path.name)
        return BPMResult(bpm=0.0, confidence=0.0)

    if essentia_bpm <= 0:
        logger.debug("Essentia failed, using beat_this result: %.1f BPM", beat_this_bpm)
        return BPMResult(bpm=beat_this_bpm, confidence=beat_this_conf)

    if beat_this_bpm <= 0:
        logger.debug("beat_this failed, using Essentia result: %.1f BPM", essentia_bpm)
        return essentia_result

    # Both algorithms produced results - combine them
    bpm_diff = abs(essentia_bpm - beat_this_bpm)

    if bpm_diff <= 5.0:
        # Algorithms agree: use weighted average and boost confidence
        total_conf = essentia_conf + beat_this_conf
        if total_conf > 0:
            combined_bpm = (
                essentia_bpm * essentia_conf + beat_this_bpm * beat_this_conf
            ) / total_conf
        else:
            combined_bpm = (essentia_bpm + beat_this_bpm) / 2.0

        # Boost confidence when algorithms agree
        combined_conf = min(1.0, (essentia_conf + beat_this_conf) / 2.0 + 0.1)

        logger.debug(
            "Cascade: Algorithms agree (diff=%.1f). "
            "Essentia=%.1f (%.2f), beat_this=%.1f (%.2f) -> Combined=%.1f (%.2f)",
            bpm_diff,
            essentia_bpm,
            essentia_conf,
            beat_this_bpm,
            beat_this_conf,
            combined_bpm,
            combined_conf,
        )
        return BPMResult(bpm=combined_bpm, confidence=combined_conf)

    # Algorithms disagree: use the one with higher confidence
    if essentia_conf >= beat_this_conf:
        logger.debug(
            "Cascade: Algorithms disagree (diff=%.1f). "
            "Preferring Essentia=%.1f (%.2f) over beat_this=%.1f (%.2f)",
            bpm_diff,
            essentia_bpm,
            essentia_conf,
            beat_this_bpm,
            beat_this_conf,
        )
        # Reduce confidence due to disagreement
        return BPMResult(bpm=essentia_bpm, confidence=max(0.0, essentia_conf - 0.1))

    logger.debug(
        "Cascade: Algorithms disagree (diff=%.1f). "
        "Preferring beat_this=%.1f (%.2f) over Essentia=%.1f (%.2f)",
        bpm_diff,
        beat_this_bpm,
        beat_this_conf,
        essentia_bpm,
        essentia_conf,
    )
    # Reduce confidence due to disagreement
    return BPMResult(bpm=beat_this_bpm, confidence=max(0.0, beat_this_conf - 0.1))
