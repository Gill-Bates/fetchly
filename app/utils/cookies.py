#!/usr/bin/env python3
#
# app/utils/cookies.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Location of the platform cookie files.

Cookie files are managed entirely through Settings -> Integrations (see
app/routes/cookies.py): they are uploaded per platform and always stored in a
dedicated ``cookies`` subdirectory of the persistent data volume, kept separate
from the rest of DATA_DIR so operators can mount/back up/inspect them (they
carry live login sessions) without wading through job artifacts.
"""

from pathlib import Path
from typing import Final

from .fs import get_data_dir

_DATA_COOKIES_DIR: Final[Path] = get_data_dir() / "cookies"


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
    """Create the dedicated data-volume cookies directory if it is missing.

    Called once at application startup (see app/main.py's lifespan) so the
    directory exists from the first run, the same way DATA_DIR itself and
    DATA_DIR/downloads are created. Idempotent and safe to call repeatedly.
    """
    _DATA_COOKIES_DIR.mkdir(parents=True, exist_ok=True)
