#!/usr/bin/env python3
#
# app/bpm_naming.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared rule for folding a detected tempo into a download filename.

Files keep their plain names on disk - the MP3 cache and the Lalal.ai stem
lookup both key off them. Only the name handed to the browser carries the BPM
tag, so a download always says what the Jobs list already shows.
"""

from pathlib import Path
from typing import Any, Final

# The tag goes right after the title so the internal ``.source`` marker - and
# whatever a caller appends behind it, such as a Lalal.ai ``_vocals`` - stays
# at the end of the name where it belongs.
SOURCE_MARKER: Final[str] = ".source"


def apply_bpm_tag(base_name: str, bpm: Any) -> str:
    """Return *base_name* with a ``_<bpm>bpm`` tag after its title part.

    ``Some Track.source`` + 94 BPM becomes ``Some Track_94bpm.source``; a plain
    ``Some Track`` becomes ``Some Track_94bpm``. A job whose analysis found no
    usable tempo - and every video job, which is never analysed - keeps its
    name unchanged.
    """
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)):
        return base_name
    rounded = round(bpm)
    if rounded <= 0:
        return base_name

    tag = f"_{rounded}bpm"
    if base_name.endswith(SOURCE_MARKER):
        return f"{base_name[: -len(SOURCE_MARKER)]}{tag}{SOURCE_MARKER}"
    return f"{base_name}{tag}"


def tagged_download_name(path: Path, bpm: Any) -> str:
    """Name *path* for download with the detected tempo folded into it."""
    return f"{apply_bpm_tag(path.stem, bpm)}{path.suffix}"
