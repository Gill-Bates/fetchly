#!/usr/bin/env python3
#
# app/integrations/lalal/policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared product rules for Lalal.ai processing."""

from typing import Any, Final

from ...audio.naming import apply_bpm_tag

LALAL_MAX_DURATION_SECONDS: Final[int] = 600
LALAL_MAX_DURATION_MINUTES: Final[int] = LALAL_MAX_DURATION_SECONDS // 60
# Stored in the settings table while the account balance is not known (db.py keeps the same default).
LALAL_MINUTES_UNKNOWN: Final[int] = -1


def minutes_from_setting(value: Any) -> float | None:
    """Read the cached balance setting; None when the balance is unknown."""
    # Not `value or UNKNOWN`: 0 is a real, exhausted balance.
    minutes = float(LALAL_MINUTES_UNKNOWN if value is None else value)
    return minutes if minutes >= 0 else None


def minutes_to_setting(minutes: float | None) -> float:
    """Encode a balance for the settings table; None becomes the unknown sentinel."""
    return LALAL_MINUTES_UNKNOWN if minutes is None else minutes


def stem_download_name(base_name: str, stem: str, bpm: Any) -> str:
    """Name a Lalal.ai stem for download, tagged with the detected tempo.

    ``Some Track.source`` + 94 BPM becomes ``Some Track_94bpm.source_vocals.mp3``.
    The stem keeps its plain ``{base_name}_{stem}.mp3`` name on disk, which is
    what the cache lookup keys off.
    """
    return f"{apply_bpm_tag(base_name, bpm)}_{stem}.mp3"
