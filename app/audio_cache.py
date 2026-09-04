#!/usr/bin/env python3
#
# app/audio_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import sqlite3
from typing import TypedDict

from .db import get_audio_analysis_cache, upsert_audio_analysis_cache

logger = logging.getLogger(__name__)


class CachedAnalysis(TypedDict):
    bpm: int | None
    bpm_confidence: float | None


def get_cached(hash_value: str) -> CachedAnalysis | None:
    """Cached audio analysis for a hash, or ``None`` on a miss."""
    row = get_audio_analysis_cache(hash_value)
    if row is None:
        return None

    return {
        "bpm": int(row["bpm"]) if row["bpm"] is not None else None,
        "bpm_confidence": float(row["bpm_confidence"]) if row["bpm_confidence"] is not None else None,
    }


def store_cache(
    hash_value: str,
    bpm: int | None,
    bpm_confidence: float | None,
) -> bool:
    """Persist audio analysis to the cache.

    Returns ``False`` (logged, never raises) on invalid input or a write error.
    """
    if not hash_value:
        logger.warning("Skipping cache write: empty hash value")
        return False

    if bpm is not None and bpm <= 0:
        logger.warning("Invalid BPM %s for hash %s; storing as None", bpm, hash_value)
        bpm = None

    try:
        upsert_audio_analysis_cache(
            hash_value,
            bpm=bpm,
            bpm_confidence=bpm_confidence,
        )
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Failed to store audio cache for hash %s: %s", hash_value, exc)
        return False
