#!/usr/bin/env python3
#
# app/utils/platform.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Platform detection and URL validation for supported video sources.

The platform is derived *exclusively* from the URL — there are no UI toggles.
Supported platforms: YouTube, TikTok, Instagram (all extracted via yt-dlp).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .youtube import strip_zwsp, validate_youtube_url

PLATFORM_YOUTUBE = "youtube"
PLATFORM_TIKTOK = "tiktok"
PLATFORM_INSTAGRAM = "instagram"

# Short, human-facing labels used for the title-row pill.
_PLATFORM_LABELS = {
    PLATFORM_YOUTUBE: "YT",
    PLATFORM_TIKTOK: "TikTok",
    PLATFORM_INSTAGRAM: "Insta",
}

_INSTAGRAM_EXACT_HOSTS = frozenset({"instagr.am", "www.instagr.am"})
_TIKTOK_ID_RE = re.compile(r"^\d{8,}$")
_TIKTOK_SHARE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _clean_url(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    return strip_zwsp(url.strip().replace("&amp;", "&"))


def detect_platform(url: str | None) -> str | None:
    """Return the platform identifier for a URL, or None if unsupported.

    Detection is host-based and case-insensitive. Subdomains (www, m, vm, vt,
    music, …) are matched via suffix checks.
    """
    value = _clean_url(url)
    if not value or not value.lower().startswith(("http://", "https://")):
        return None

    try:
        host = (urlparse(value).hostname or "").lower()
    except ValueError:
        return None

    if not host:
        return None

    if host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com"):
        return PLATFORM_YOUTUBE
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return PLATFORM_TIKTOK
    if host == "instagram.com" or host.endswith(".instagram.com") or host in _INSTAGRAM_EXACT_HOSTS:
        return PLATFORM_INSTAGRAM

    return None


def platform_label(platform: str | None) -> str:
    """Return the short pill label for a platform identifier (default: empty)."""
    return _PLATFORM_LABELS.get(platform or "", "")


def _validate_tiktok_url(url: str) -> tuple[bool, str]:
    """Conservative structural validation for TikTok media/share URLs."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    segments = [seg for seg in (parsed.path or "").strip("/").split("/") if seg]

    if not segments:
        return False, "Invalid TikTok URL. Expected a TikTok video, photo, or share link."

    # Long form: /@user/video/<id> and /@user/photo/<id>
    if len(segments) >= 3 and segments[0].startswith("@") and segments[1] in {"video", "photo"}:
        if _TIKTOK_ID_RE.fullmatch(segments[2]):
            return True, ""

    # Share hosts: vm.tiktok.com/<code>, vt.tiktok.com/<code>
    if host.startswith(("vm.", "vt.")) and _TIKTOK_SHARE_CODE_RE.fullmatch(segments[0]):
        return True, ""

    # Redirect/share path: /t/<code>
    if segments[0] == "t" and len(segments) >= 2 and _TIKTOK_SHARE_CODE_RE.fullmatch(segments[1]):
        return True, ""

    return False, "Invalid TikTok URL. Expected a TikTok video, photo, or share link."


def _validate_instagram_url(url: str) -> tuple[bool, str]:
    """Light structural check for Instagram post/reel URLs."""
    parsed = urlparse(url)
    segments = [seg for seg in (parsed.path or "").strip("/").split("/") if seg]
    # Accept /p/<code>, /reel/<code>, /tv/<code>.
    if segments and segments[0] in {"p", "reel", "tv"} and len(segments) > 1:
        return True, ""
    return False, "Invalid Instagram URL. Expected an instagram.com post, reel, or tv link."


def validate_media_url(url: str | None) -> tuple[bool, str]:
    """Validate a URL against all supported platforms.

    Returns:
        (is_valid, error_message). The error message is empty when valid.
    """
    if not url or not isinstance(url, str):
        return False, "URL is required"

    value = _clean_url(url)
    if not value:
        return False, "URL is required"
    if len(value) > 2048:
        return False, "URL is too long"
    if not value.lower().startswith(("http://", "https://")):
        return False, "URL must start with http:// or https://"

    platform = detect_platform(value)
    if platform == PLATFORM_YOUTUBE:
        return validate_youtube_url(value)
    if platform == PLATFORM_TIKTOK:
        return _validate_tiktok_url(value)
    if platform == PLATFORM_INSTAGRAM:
        return _validate_instagram_url(value)

    return False, "Unsupported URL. Supported platforms: YouTube, TikTok, Instagram."
