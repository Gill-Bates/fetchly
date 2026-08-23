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

_HASH_CHUNK_SIZE: Final[int] = 1024 * 1024


def compute_audio_hash(path: Path) -> str:
    """Compute a fast, stable cache key for an audio file.

    The digest covers the complete file content in bounded chunks. This makes
    the cache key a reliable content hash rather than a boundary sample.

    Raises:
        FileNotFoundError: If *path* does not point to a regular file.
        OSError: If the file cannot be read (permission error, I/O error, etc.).
    """
    if not path.is_file():
        raise FileNotFoundError(f"Cannot hash non-existent file: {path}")

    digest = hashlib.sha256()

    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)

    return digest.hexdigest()
