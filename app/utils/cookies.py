#!/usr/bin/env python3
#
# app/utils/cookies.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Location of the platform cookie files.

Managed through Settings -> Integrations and stored per platform in a
dedicated ``cookies`` subdirectory of the data volume, kept apart from job
artifacts so operators can mount/back up/inspect these live-login files.
"""

import logging
from pathlib import Path
from typing import Final

from .fs import get_data_dir

logger = logging.getLogger(__name__)

_DATA_COOKIES_DIR: Final[Path] = get_data_dir() / "cookies"

# Owner-only. The jars are 0600, but a readable directory still leaks the
# listing (which platforms are signed in, last refresh). Matches the container
# entrypoint's chmod; only affects bare-metal installs.
_DIR_MODE: Final[int] = 0o700


def find_cookie_file(filename: str) -> Path | None:
    """Return the cookie file for ``filename`` if it exists, else ``None``."""
    if not filename:
        return None
    candidate = _DATA_COOKIES_DIR / filename
    return candidate if candidate.is_file() else None


def default_cookie_file(filename: str) -> Path:
    """Return the canonical path for a cookie filename (may not exist yet)."""
    return _DATA_COOKIES_DIR / filename


def ensure_data_cookies_dir() -> None:
    """Create the cookies directory if missing (called once at startup).

    Permissions are re-asserted every call: mkdir's ``mode`` only applies on
    creation, so an install first run under a permissive umask would else keep
    a world-readable directory. Tightening only removes access, so it is safe.
    """
    _DATA_COOKIES_DIR.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

    try:
        if _DATA_COOKIES_DIR.stat().st_mode & 0o077:
            _DATA_COOKIES_DIR.chmod(_DIR_MODE)
            logger.info("Restricted cookie directory %s to 0700", _DATA_COOKIES_DIR)
    except OSError as exc:
        # Volume owned by another account: warn, don't refuse startup - the
        # jars are still written 0600.
        logger.warning("Could not restrict permissions of %s: %s", _DATA_COOKIES_DIR, exc)
