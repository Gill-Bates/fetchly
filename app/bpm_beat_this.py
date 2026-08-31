#!/usr/bin/env python3
#
# app/bpm_beat_this.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

"""BPM detection using beat_this with optional DBN postprocessing.

beat_this is a state-of-the-art beat tracker from CPJKU/beat_this that
provides accurate beat and downbeat detection. BPM is derived from the
median inter-beat interval.

When DBN postprocessing is enabled (requires madmom), the model uses a
Dynamic Bayesian Network to smooth beat predictions.
"""

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

import numpy as np

from .bpm_normalization import normalize_bpm

if TYPE_CHECKING:
    from beat_this.inference import File2Beats

logger = logging.getLogger(__name__)

# Cache the model to avoid reloading on every call
_model_lock = threading.RLock()
_model_instance: "File2Beats | None" = None
_DBN_DEFAULT_ENABLED: Final[bool] = True

# Minimum number of beats required for reliable BPM calculation
_MIN_BEATS_FOR_BPM: Final[int] = 4


class BeatThisResult(NamedTuple):
    """Result of beat_this BPM detection."""

    bpm: float
    confidence: float
    num_beats: int


def _dbn_enabled() -> bool:
    """Return whether DBN postprocessing can be enabled in this runtime."""
    if not _DBN_DEFAULT_ENABLED:
        return False

    try:
        import madmom  # noqa: F401
    except ImportError:
        logger.info("madmom not available, disabling beat_this DBN postprocessing")
        return False

    return True


def _get_model() -> "File2Beats":
    """Return the cached File2Beats model instance (thread-safe)."""
    global _model_instance

    with _model_lock:
        if _model_instance is not None:
            return _model_instance

        # Lazy import to avoid loading heavy libraries at module level
        from beat_this.inference import File2Beats

        use_dbn = _dbn_enabled()

        logger.info(
            "Loading beat_this model (dbn=%s)...",
            use_dbn,
        )

        # Use CPU by default for compatibility; GPU detection could be added
        _model_instance = File2Beats(
            checkpoint_path="final0",
            device="cpu",
            dbn=use_dbn,
        )

        logger.info("beat_this model loaded successfully")
        return _model_instance


def _bpm_from_beats(beats: np.ndarray) -> tuple[float, float]:
    """Calculate BPM from an array of beat timestamps.

    Args:
        beats: Array of beat timestamps in seconds.

    Returns:
        Tuple of (bpm, confidence) where confidence is based on
        inter-beat interval consistency.
    """
    if len(beats) < _MIN_BEATS_FOR_BPM:
        return 0.0, 0.0

    # Calculate inter-beat intervals
    intervals = np.diff(beats)

    if len(intervals) == 0:
        return 0.0, 0.0

    # Use median interval for robustness against outliers
    median_interval = float(np.median(intervals))

    if median_interval <= 0:
        return 0.0, 0.0

    # Convert interval to BPM
    raw_bpm = 60.0 / median_interval

    # Calculate confidence based on interval consistency (IQR-based)
    q1, q3 = np.percentile(intervals, [25, 75])
    iqr = q3 - q1
    # Confidence is higher when intervals are consistent (low IQR relative to median)
    normalized_iqr = iqr / median_interval
    # Map to 0-1 range: perfect consistency = 1.0, very inconsistent = 0.0
    confidence = max(0.0, min(1.0, 1.0 - normalized_iqr))

    return raw_bpm, confidence


def extract_bpm_beat_this(audio_path: Path) -> BeatThisResult:
    """Extract BPM using beat_this.

    DBN postprocessing is used when madmom is available; otherwise raw beat
    predictions are used directly.

    Args:
        audio_path: Path to audio file (any format supported by torchaudio/ffmpeg).

    Returns:
        BeatThisResult with detected BPM, confidence, and beat count.

    Raises:
        FileNotFoundError: If the audio file doesn't exist.
        RuntimeError: If beat detection fails.
    """
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    try:
        model = _get_model()

        with _model_lock:
            # Shared PyTorch model instances are not safe for concurrent forward passes.
            beats, _downbeats = model(str(audio_path))

    except ImportError as exc:
        logger.info("beat_this not available: %s", exc)
        return BeatThisResult(bpm=0.0, confidence=0.0, num_beats=0)
    except MemoryError:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logger.exception("beat_this inference failed for %s", audio_path.name)
        raise RuntimeError(f"beat_this inference failed for {audio_path.name}") from exc

    if len(beats) < _MIN_BEATS_FOR_BPM:
        logger.debug(
            "beat_this: Too few beats detected (%d) for %s",
            len(beats),
            audio_path.name,
        )
        return BeatThisResult(bpm=0.0, confidence=0.0, num_beats=len(beats))

    # Calculate BPM from beat timestamps
    raw_bpm, confidence = _bpm_from_beats(beats)

    # Normalize to standard range
    bpm = normalize_bpm(raw_bpm)

    logger.debug(
        "beat_this: %.1f BPM (raw=%.1f, confidence=%.2f, beats=%d) for %s",
        bpm,
        raw_bpm,
        confidence,
        len(beats),
        audio_path.name,
    )

    return BeatThisResult(bpm=bpm, confidence=confidence, num_beats=len(beats))


def is_beat_this_available() -> bool:
    """Check if beat_this is installed and usable."""
    try:
        from beat_this.inference import File2Beats

        return True
    except ImportError:
        return False
