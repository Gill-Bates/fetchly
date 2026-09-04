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
from pathlib import Path
from typing import Any, Final

from fastapi import HTTPException, Request

__all__ = [
    "AUDIO_SOURCE_EXTENSIONS",
    "TRIM_ID_RE",
    "get_data_dir",
    "get_json_body",
    "path_is_file",
    "resolve_within_root",
]

# Extensions yt-dlp may return for pre-transcode source audio; named for the
# use, not a codec guarantee (some are lossy, some are containers).
AUDIO_SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".opus", ".m4a", ".webm", ".ogg", ".aac", ".flac", ".wav",
})

# ASCII digits only (trim IDs are f"{int}_{int}"). Unanchored: call sites use
# fullmatch(), which also rejects a trailing newline (unlike match() + "$").
TRIM_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+_[0-9]+")


def get_data_dir() -> Path:
    """The application data directory (the ``DATA_DIR`` env var, else ./data)."""
    configured = os.environ.get("DATA_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path(__file__).parent.parent.parent / "data").resolve()


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
