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
]

# Extensions yt-dlp may hand back for the pre-transcode source audio. Not all
# lossless (.opus, .aac are lossy; .m4a/.ogg/.webm are containers that can
# hold either) - named for what they're used for, not for a codec guarantee.
AUDIO_SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".opus", ".m4a", ".webm", ".ogg", ".aac", ".flac", ".wav",
})

# ASCII digits only - trim IDs are always generated as f"{int(...)}_{int(...)}"
# (see app/routes/trim.py), so a Unicode digit here could only be an input the
# app itself never produced. Deliberately unanchored: every call site uses
# fullmatch(), which already requires the whole string to match and - unlike
# match() with a trailing "$" - rejects a trailing newline.
TRIM_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+_[0-9]+")


def get_data_dir() -> Path:
    """Return the configured application data directory.

    ``DATA_DIR`` is the single canonical setting; it is set by the container
    entrypoint and may be overridden for local runs.
    """
    configured = os.environ.get("DATA_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path(__file__).parent.parent.parent / "data").resolve()


async def path_is_file(path: Path) -> bool:
    """Async wrapper around Path.is_file() for use in async route handlers."""
    return await asyncio.to_thread(path.is_file)


# Every current caller sends a small settings/auth payload; Caddy's own
# request_body limit (100MB, see Caddyfile) is sized for uploads elsewhere and
# is not a substitute for a boundary sized to what this helper actually parses.
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
