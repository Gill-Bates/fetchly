#!/usr/bin/env python3
#
# app/audio_hash.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

__all__ = ["compute_audio_hash"]

# Sample only the boundaries of the file so that large FLAC/MP3 files do not
# block the analysis worker for hundreds of milliseconds on every cache lookup.
# File size is included in the digest to distinguish files of different lengths
# that happen to share identical first/last blocks.
_SAMPLE_SIZE: Final[int] = 64 * 1024  # 64 KiB per boundary


def compute_audio_hash(path: Path) -> str:
    """Compute a fast, stable cache key for an audio file.

    The digest covers the file size, the first ``_SAMPLE_SIZE`` bytes, and the
    last ``_SAMPLE_SIZE`` bytes (when the file is large enough).  This avoids
    reading the entire file while still producing distinct hashes for tracks
    with different audio content.

    Raises:
        FileNotFoundError: If *path* does not point to a regular file.
        OSError: If the file cannot be read (permission error, I/O error, etc.).
    """
    if not path.is_file():
        raise FileNotFoundError(f"Cannot hash non-existent file: {path}")

    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode())

    with path.open("rb") as fh:
        digest.update(fh.read(_SAMPLE_SIZE))
        if stat.st_size > _SAMPLE_SIZE * 2:
            fh.seek(-_SAMPLE_SIZE, 2)
            digest.update(fh.read(_SAMPLE_SIZE))

    return digest.hexdigest()