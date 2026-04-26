#!/usr/bin/env python3
#
# app/utils/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Version and build information for tubeyou."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_version() -> str:
    """Get application version from VERSION file. Falls back to 'dev'."""
    try:
        version_file = Path(__file__).resolve().parent.parent.parent / "VERSION"
        if version_file.exists():
            return version_file.read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        pass
    return "dev"


@lru_cache(maxsize=1)
def get_build_info() -> str:
    """Get build info (Git commit hash) from BUILD_INFO file. Falls back to 'dev'."""
    try:
        build_file = Path(__file__).resolve().parent.parent.parent / "BUILD_INFO"
        if build_file.exists():
            return build_file.read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        pass
    return "dev"


VERSION = get_version()
BUILD_INFO = get_build_info()