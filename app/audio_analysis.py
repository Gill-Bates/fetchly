#!/usr/bin/env python3
#
# app/audio_analysis.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

from .bpm import BPMResult, extract_bpm_cascade

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AudioAnalysisResult:
    bpm: int | None
    confidence: float | None


def _to_int_bpm(value: Real | object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None

    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        return None

    return round(numeric_value)


def _to_confidence(value: Real | object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None

    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        return None

    # Keep DB/UI payloads stable without preserving meaningless float noise.
    return round(numeric_value, 4)

def extract_analysis(path: Path) -> AudioAnalysisResult:
    """Run the configured BPM cascade and normalize result types for storage."""
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    bpm_result: BPMResult = extract_bpm_cascade(path)

    bpm = _to_int_bpm(bpm_result.bpm)
    confidence = _to_confidence(bpm_result.confidence)

    return AudioAnalysisResult(
        bpm=bpm,
        confidence=confidence,
    )
