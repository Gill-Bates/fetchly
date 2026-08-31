#!/usr/bin/env python3
#
# app/lalal_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared product rules for Lalal.ai processing."""

from typing import Any, Final

LALAL_MAX_DURATION_SECONDS: Final[int] = 600
LALAL_MAX_DURATION_MINUTES: Final[int] = LALAL_MAX_DURATION_SECONDS // 60

# A stem keeps its plain on-disk name (`{base_name}_{stem}.mp3`) because the
# cache lookup keys off it - only the name handed to the browser carries the
# BPM tag, placed right after the title so `.source_<stem>` stays at the end.
SOURCE_MARKER: Final[str] = ".source"


def stem_download_name(base_name: str, stem: str, bpm: Any) -> str:
    """Name a Lalal.ai stem for download, tagged with the detected tempo.

    ``Some Track.source`` + 94 BPM becomes ``Some Track_94bpm.source_vocals.mp3``.
    A job whose analysis found no usable tempo keeps the untagged name.
    """
    tagged = base_name
    if not isinstance(bpm, bool) and isinstance(bpm, (int, float)):
        rounded = round(bpm)
        if rounded > 0:
            tag = f"_{rounded}bpm"
            if base_name.endswith(SOURCE_MARKER):
                head = base_name[: -len(SOURCE_MARKER)]
                tagged = f"{head}{tag}{SOURCE_MARKER}"
            else:
                tagged = f"{base_name}{tag}"
    return f"{tagged}_{stem}.mp3"
