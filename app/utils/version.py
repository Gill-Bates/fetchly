#!/usr/bin/env python3
#
# app/utils/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Version and build information for tubeyou."""

from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# Pure path arithmetic – no I/O occurs here.
_PROJECT_ROOT = Path(__file__).absolute().parent.parent.parent


def _read_file(name: str) -> str | None:
    """Read *name* from the project root. Return None if missing or unreadable."""
    try:
        return (_PROJECT_ROOT / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Unable to read %s from %s: %s", name, _PROJECT_ROOT, e)
        return None


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return application version from the VERSION file, or 'dev'."""
    return _read_file("VERSION") or "dev"


@lru_cache(maxsize=1)
def get_build_info() -> str:
    """Return build info from the BUILD_INFO file, or 'dev'."""
    return _read_file("BUILD_INFO") or "dev"


@lru_cache(maxsize=1)
def get_ytdlp_version() -> str:
    """Return installed yt-dlp version, or 'unavailable' on failure."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Unable to read yt-dlp version: %s", exc)
        return "unavailable"

    version = (result.stdout or "").strip()
    return version or "unavailable"


# --------------------------------------------------------------------------- #
# Lazy module-level constants – I/O is deferred until first attribute access.
# --------------------------------------------------------------------------- #
def __getattr__(name: str) -> str:
    """Lazy-load VERSION and BUILD_INFO on first access."""
    if name == "VERSION":
        return get_version()
    if name == "BUILD_INFO":
        return get_build_info()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include lazy attributes in dir() output."""
    return sorted({*globals(), "VERSION", "BUILD_INFO"})


# Type-checker stubs so that `version.VERSION` is known to be `str`.
if TYPE_CHECKING:
    VERSION: str
    BUILD_INFO: str