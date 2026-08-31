#!/usr/bin/env python3
#
# app/lalal_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared product rules for Lalal.ai processing."""

from typing import Any, Final

from .bpm_naming import apply_bpm_tag

LALAL_MAX_DURATION_SECONDS: Final[int] = 600
LALAL_MAX_DURATION_MINUTES: Final[int] = LALAL_MAX_DURATION_SECONDS // 60


def stem_download_name(base_name: str, stem: str, bpm: Any) -> str:
    """Name a Lalal.ai stem for download, tagged with the detected tempo.

    ``Some Track.source`` + 94 BPM becomes ``Some Track_94bpm.source_vocals.mp3``.
    The stem keeps its plain ``{base_name}_{stem}.mp3`` name on disk, which is
    what the cache lookup keys off.
    """
    return f"{apply_bpm_tag(base_name, bpm)}_{stem}.mp3"
