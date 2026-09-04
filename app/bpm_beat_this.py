#!/usr/bin/env python3
#
# app/bpm_beat_this.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""BPM detection using beat_this.

beat_this is a state-of-the-art beat tracker from CPJKU/beat_this. BPM is
derived from the median inter-beat interval.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

import numpy as np

from .bpm_normalization import normalize_bpm

if TYPE_CHECKING:
    from beat_this.inference import File2Beats

logger = logging.getLogger(__name__)

_model_lock = threading.RLock()
_model_instance: File2Beats | None = None  # cached across calls
_MIN_BEATS_FOR_BPM: Final[int] = 4


class BeatThisResult(NamedTuple):
    bpm: float
    confidence: float
    num_beats: int


def _get_model() -> File2Beats:
    """Return the cached File2Beats model instance (thread-safe)."""
    global _model_instance

    with _model_lock:
        if _model_instance is not None:
            return _model_instance

        from beat_this.inference import File2Beats  # heavy; imported lazily

        logger.info("Loading beat_this model...")

        # CPU by default for compatibility.
        _model_instance = File2Beats(
            checkpoint_path="final0",
            device="cpu",
            dbn=False,
        )

        logger.info("beat_this model loaded successfully")
        return _model_instance


def _bpm_from_beats(beats: np.ndarray) -> tuple[float, float]:
    """Return ``(bpm, confidence)`` from beat timestamps (seconds).

    BPM is 60 / median inter-beat interval; confidence falls as the interval
    IQR (relative to the median) grows.
    """
    if len(beats) < _MIN_BEATS_FOR_BPM:
        return 0.0, 0.0

    intervals = np.diff(beats)
    if len(intervals) == 0:
        return 0.0, 0.0

    median_interval = float(np.median(intervals))
    if median_interval <= 0:
        return 0.0, 0.0

    raw_bpm = 60.0 / median_interval

    q1, q3 = np.percentile(intervals, [25, 75])
    normalized_iqr = (q3 - q1) / median_interval
    confidence = max(0.0, min(1.0, 1.0 - normalized_iqr))

    return raw_bpm, confidence


def extract_bpm_beat_this(audio_path: Path) -> BeatThisResult:
    """Extract BPM from an audio file using beat_this.

    Raises FileNotFoundError if the file is missing, RuntimeError if detection
    fails.
    """
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    try:
        model = _get_model()

        with _model_lock:
            # A shared PyTorch model is not safe for concurrent forward passes.
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

    raw_bpm, confidence = _bpm_from_beats(beats)
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
        from beat_this.inference import File2Beats  # noqa: F401  # import *is* the probe

        return True
    except ImportError:
        return False
