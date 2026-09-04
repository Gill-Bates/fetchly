#!/usr/bin/env python3
#
# app/bpm.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# BPM (beats per minute) detection for audio files using Essentia.
# Callers are responsible for concurrency control.

import logging
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Final, NamedTuple

from .bpm_normalization import normalize_bpm

logger = logging.getLogger(__name__)

# Analyze only the first 2 minutes of long files.
_MAX_ANALYSIS_DURATION: Final[int] = 120
_FFMPEG_ERROR_TAIL_BYTES: Final[int] = 500
_FFMPEG_TIMEOUT_SECONDS: Final[int] = 120
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
) -> None:
    """Decode to mono 44.1 kHz with optional ``-af`` filters and ``-t`` cap.

    Raises RuntimeError on non-zero exit or timeout.
    """
    cmd = ["ffmpeg", "-y"]

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


def _extract_bpm_essentia(audio_path: Path) -> BPMResult:
    """Extract BPM from a mono WAV using Essentia's RhythmExtractor2013."""
    from essentia.standard import MonoLoader  # heavy; imported lazily

    loader = MonoLoader(filename=str(audio_path))
    audio = loader()

    bpm, _, confidence, _, _ = _get_rhythm_extractor()(audio)

    bpm = float(normalize_bpm(bpm))
    confidence = float(confidence)

    return BPMResult(bpm=bpm, confidence=confidence)


def extract_bpm(
    file_path: Path,
    *,
    max_duration: int = _MAX_ANALYSIS_DURATION,
) -> BPMResult:
    """Extract BPM from an audio file in a single analysis pass.

    Decodes to mono 44.1 kHz WAV (highpass only), runs RhythmExtractor2013,
    normalizes into the 70-180 range, and forces ``bpm=0.0`` when the result
    is implausible or below ``_MIN_CONFIDENCE``.

    Raises FileNotFoundError if the file is missing, RuntimeError if a step
    fails.
    """
    _ensure_file_exists(file_path)

    with tempfile.TemporaryDirectory(prefix="bpm_") as tmp:
        tmp_dir = Path(tmp)
        clean_wav = tmp_dir / "clean.wav"

        # highpass=f=40 removes sub-bass rumble that confuses beat detection.
        # No loudnorm: it distorts transients and hurts detection.
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


def extract_bpm_cascade(
    file_path: Path,
    *,
    max_duration: int = _MAX_ANALYSIS_DURATION,
) -> BPMResult:
    """Extract BPM by cascading Essentia and beat_this.

    Runs Essentia RhythmExtractor2013, then beat_this (with optional DBN
    postprocessing). Within 5 BPM, results are confidence-weighted averaged and
    confidence is boosted; otherwise the higher-confidence result wins.
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
            # No highpass here, unlike step 1: beat_this is a trained model with
            # its own front end and expects the untouched spectrum, while the
            # rumble filter exists for Essentia's onset detection. Both results
            # end up comparable because each detector normalizes its tempo into
            # the same range (see bpm_normalization.normalize_bpm).
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

    if essentia_bpm <= 0 and beat_this_bpm <= 0:
        logger.debug("Both algorithms failed for %s", file_path.name)
        return BPMResult(bpm=0.0, confidence=0.0)

    if essentia_bpm <= 0:
        logger.debug("Essentia failed, using beat_this result: %.1f BPM", beat_this_bpm)
        return BPMResult(bpm=beat_this_bpm, confidence=beat_this_conf)

    if beat_this_bpm <= 0:
        logger.debug("beat_this failed, using Essentia result: %.1f BPM", essentia_bpm)
        return essentia_result

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
