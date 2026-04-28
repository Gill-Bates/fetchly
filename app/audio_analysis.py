#!/usr/bin/env python3
#
# app/audio_analysis.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .bpm import extract_bpm

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AudioAnalysisResult:
    bpm: int | None
    confidence: float | None

def extract_analysis(path: Path) -> AudioAnalysisResult:
    """Run BPM analysis for a downloaded audio file."""
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    bpm_result = extract_bpm(path)

    raw_bpm = getattr(bpm_result, "bpm", None)
    raw_confidence = getattr(bpm_result, "confidence", None)

    bpm = int(round(raw_bpm)) if isinstance(raw_bpm, (int, float)) and raw_bpm > 0 else None
    confidence = round(float(raw_confidence), 4) if isinstance(raw_confidence, (int, float)) else None

    return AudioAnalysisResult(
        bpm=bpm,
        confidence=confidence,
    )