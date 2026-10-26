#!/usr/bin/env python3
#
# app/utils/cookie_args.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The one gate deciding whether a platform's cookie jar is handed to yt-dlp."""

from __future__ import annotations

from pathlib import Path

from .cookie_status import cookie_file_is_usable
from .cookies import find_cookie_file
from .platform import PLATFORM_COOKIE_FILENAMES, detect_platform

__all__ = ["cookie_args_for_jar", "cookie_args_for_url"]


def cookie_args_for_jar(platform: str, cookie_path: Path | None) -> list[str]:
    """``--cookies <path>`` only for an existing, usable jar; otherwise nothing.

    An all-expired or login-less jar is left out on purpose: yt-dlp loads
    cookies with ignore_expires=True and would send the dead session anyway,
    and platforms block a stale login harder than an anonymous request.
    """
    if cookie_path is None or not cookie_path.is_file():
        return []
    if not cookie_file_is_usable(cookie_path, platform):
        return []
    return ["--cookies", str(cookie_path)]


def cookie_args_for_url(url: str) -> list[str]:
    """Cookie arguments for a URL's platform, or ``[]`` when it has no usable jar."""
    platform = detect_platform(url)
    filename = PLATFORM_COOKIE_FILENAMES.get(platform or "")
    if not platform or not filename:
        return []
    return cookie_args_for_jar(platform, find_cookie_file(filename))
