#!/usr/bin/env python3
#
# app/bpm_normalization.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared BPM normalization policy for all tempo detectors."""

import math
from typing import Final

BPM_MIN: Final[float] = 70.0
BPM_MAX: Final[float] = 180.0


def normalize_bpm(bpm: float) -> float:
    """Normalize a detector result into the supported musical BPM range.

    Tempo detectors commonly return binary multiples of the perceived tempo.
    Non-positive and non-finite detector results are represented as ``0.0``.
    """
    if not math.isfinite(bpm) or bpm <= 0:
        return 0.0

    if bpm < BPM_MIN:
        bpm *= 2 ** math.ceil(math.log2(BPM_MIN / bpm))
    elif bpm > BPM_MAX:
        bpm /= 2 ** math.ceil(math.log2(bpm / BPM_MAX))

    if bpm > BPM_MAX:
        bpm /= 2

    return bpm
