#!/usr/bin/env python3
#
# app/utils/fs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared filesystem and request-parsing helpers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Final

from fastapi import HTTPException, Request

__all__ = [
    "AUDIO_SOURCE_EXTENSIONS",
    "TRIM_ID_RE",
    "TRIM_WAV_GLOB",
    "get_data_dir",
    "get_json_body",
    "job_dir",
    "jobs_dir",
    "path_is_file",
    "resolve_within_root",
    "thumb_cache_dir",
    "trim_base_name",
    "trim_wav_name",
]

# Extensions yt-dlp may return for pre-transcode source audio; named for the
# use, not a codec guarantee (some are lossy, some are containers).
AUDIO_SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".opus",
        ".m4a",
        ".webm",
        ".ogg",
        ".aac",
        ".flac",
        ".wav",
    }
)

# ASCII digits only (trim IDs are f"{int}_{int}"). Unanchored: call sites use
# fullmatch(), which also rejects a trailing newline (unlike match() + "$").
TRIM_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+_[0-9]+")
TRIM_WAV_GLOB: Final = "trim_*.wav"


def trim_base_name(trim_id: str) -> str:
    """File stem of a trimmed clip; Lalal.ai stem files derive from it."""
    return f"trim_{trim_id}"


def trim_wav_name(trim_id: str) -> str:
    """File name of the trimmed WAV for a validated ``trim_id``."""
    return f"{trim_base_name(trim_id)}.wav"


def get_data_dir() -> Path:
    """The application data directory (the ``DATA_DIR`` env var, else ./data)."""
    configured = os.environ.get("DATA_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path(__file__).parent.parent.parent / "data").resolve()


def jobs_dir() -> Path:
    """Parent directory of all per-job directories (``DATA_DIR/jobs``).

    Derived solely from :func:`get_data_dir`; there is no injectable root, so no
    caller can hold a jobs root that diverges from the global one. Redirect it
    in a test through the ``DATA_DIR`` env var, the same path model production
    uses. Per-job artifacts live one level below the data dir so the sibling
    caches (``thumb-cache``, ``watermark-cache``, ``geolite2``, ``cookies``,
    ``.cache``) are never mistaken for job directories.
    """
    return get_data_dir() / "jobs"


def job_dir(job_id: str | uuid.UUID) -> Path:
    """Single source of truth for a job's on-disk directory (``DATA_DIR/jobs/<uuid>``).

    The id is validated and canonicalized to a real UUID here so no caller can
    escape the jobs root (``../cookies``) or address a non-job name: this
    helper, not its callers, enforces the invariant. The jobs root comes only
    from :func:`jobs_dir`; there is no injectable root and no fallback to the
    former ``DATA_DIR/<uuid>`` layout. Raises ``ValueError`` for anything that
    is not a UUID.
    """
    try:
        canonical_id = str(uuid.UUID(str(job_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid job id: {job_id!r}") from exc
    return jobs_dir() / canonical_id


def thumb_cache_dir() -> Path:
    """Directory of the on-disk thumbnail proxy cache (also swept by housekeeping)."""
    return get_data_dir() / "thumb-cache"


async def path_is_file(path: Path) -> bool:
    """Async wrapper around Path.is_file() for use in async route handlers."""
    return await asyncio.to_thread(path.is_file)


def resolve_within_root(candidate: Path, root: Path, *, allow_symlink: bool = True) -> Path:
    """Resolve ``candidate`` against ``root`` and verify it stays inside it.

    ``candidate`` is joined onto ``root`` when relative, or used as-is when
    absolute. ``root`` is resolved first; the final resolved path must sit
    inside it or a ``ValueError`` is raised (traversal / escape attempt).

    When ``allow_symlink`` is False, ``candidate`` is rejected *before* being
    resolved if it is itself a symlink. This matters for callers about to
    delete or overwrite the path: a dangling symlink makes ``exists()``
    report False (it follows the link and finds nothing), which would
    otherwise look like "already gone" without the link itself ever being
    removed.

    Raises ``OSError`` if resolving hits an unexpected filesystem error, and
    ``ValueError`` if the candidate is unsafe (escapes root, or is a
    disallowed symlink).
    """
    root_resolved = root.resolve()
    path = candidate if candidate.is_absolute() else root_resolved / candidate

    if not allow_symlink and path.is_symlink():
        raise ValueError(f"path is a symlink, refusing: {path}")

    resolved = path.resolve()

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"path escapes root: {resolved} not inside {root_resolved}") from None

    return resolved


# Callers send small settings/auth payloads; Caddy's 100MB body limit is for
# uploads elsewhere, not a bound on what this helper parses.
_MAX_JSON_BODY_BYTES: Final = 64 * 1024


async def get_json_body(request: Request) -> dict[str, Any]:
    """Read the request body as JSON, bounded, and ensure the payload is a dict."""
    body = bytearray()
    async for chunk in request.stream():
        remaining = _MAX_JSON_BODY_BYTES - len(body)
        if len(chunk) > remaining:
            raise HTTPException(status_code=413, detail="Request body too large")
        body.extend(chunk)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return payload
