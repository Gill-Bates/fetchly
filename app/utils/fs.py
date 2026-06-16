#!/usr/bin/env python3
#
# app/utils/fs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared filesystem and request-parsing helpers."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Final

from fastapi import HTTPException, Request

__all__ = [
    "LOSSLESS_AUDIO_SOURCE_EXTENSIONS",
    "TRIM_ID_RE",
    "get_json_body",
    "path_is_file",
]

LOSSLESS_AUDIO_SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".opus", ".m4a", ".webm", ".ogg", ".aac", ".flac", ".wav",
})

TRIM_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+_\d+$")


async def path_is_file(path: Path) -> bool:
    """Async wrapper around Path.is_file() for use in async route handlers."""
    return await asyncio.to_thread(path.is_file)



async def get_json_body(request: Request) -> dict[str, Any]:
    """Parse request body as JSON and ensure the payload is a dict."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return payload
