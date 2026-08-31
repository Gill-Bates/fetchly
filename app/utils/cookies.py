#!/usr/bin/env python3
#
# app/utils/cookies.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared lookup order for platform cookie files."""

import os
from pathlib import Path
from typing import Final

from .fs import get_data_dir

_PROJECT_COOKIES_DIR: Final[Path] = Path(__file__).parent.parent.parent
_DATA_COOKIES_DIR: Final[Path] = get_data_dir()


def cookie_search_dirs() -> tuple[Path, ...]:
    """Return cookie directories in their authoritative precedence order."""
    custom_dir = os.environ.get("FETCHLY_COOKIES_DIR", "").strip()
    if custom_dir:
        return (Path(custom_dir), _PROJECT_COOKIES_DIR, _DATA_COOKIES_DIR)
    return (_PROJECT_COOKIES_DIR, _DATA_COOKIES_DIR)


def find_cookie_file(filename: str) -> Path | None:
    """Return the first existing cookie file, or ``None`` when absent."""
    if not filename:
        return None
    for directory in cookie_search_dirs():
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def default_cookie_file(filename: str) -> Path:
    """Return the conventional project-root path for a cookie filename."""
    return _PROJECT_COOKIES_DIR / filename
