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

import logging
from pathlib import Path
from typing import Final

from .fs import get_data_dir

logger = logging.getLogger(__name__)

_DATA_COOKIES_DIR: Final[Path] = get_data_dir() / "cookies"

# Owner-only. The jars inside are published at 0600 (see app/routes/cookies.py)
# because they hold live logins; a group/world-readable directory around them
# still hands every local account the file listing - which platforms are signed
# in, and when the session was last refreshed. This matches what the container
# entrypoint already applies to DATA_DIR's subdirectories ("chmod u=rwX,go-rwx"
# in docker/entrypoint.sh), so it only changes bare-metal installs.
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
    """Create the dedicated data-volume cookies directory if it is missing.

    Called once at application startup (see app/main.py's lifespan) so the
    directory exists from the first run, the same way DATA_DIR itself and
    DATA_DIR/downloads are created. Idempotent and safe to call repeatedly.

    Permissions are re-stated on every call rather than left to mkdir: its
    ``mode`` only applies when the directory is actually created, so an
    install that first ran under a permissive umask would keep a
    world-readable directory forever. Tightening only ever removes access, so
    repeating it is safe.
    """
    _DATA_COOKIES_DIR.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

    try:
        if _DATA_COOKIES_DIR.stat().st_mode & 0o077:
            _DATA_COOKIES_DIR.chmod(_DIR_MODE)
            logger.info("Restricted cookie directory %s to 0700", _DATA_COOKIES_DIR)
    except OSError as exc:
        # A data volume owned by another account: chmod is not ours to make.
        # The jars themselves are still written 0600, so this is worth a
        # warning rather than a refused startup.
        logger.warning("Could not restrict permissions of %s: %s", _DATA_COOKIES_DIR, exc)
