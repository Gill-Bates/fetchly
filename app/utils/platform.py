#!/usr/bin/env python3
#
# app/utils/platform.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Platform detection and URL validation for supported video sources.

The platform is derived *exclusively* from the URL — there are no UI toggles.
Supported platforms: YouTube, TikTok, Instagram, Facebook (all extracted
via yt-dlp).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .youtube import strip_zwsp, validate_youtube_url

PLATFORM_YOUTUBE = "youtube"
PLATFORM_TIKTOK = "tiktok"
PLATFORM_INSTAGRAM = "instagram"
PLATFORM_FACEBOOK = "facebook"

# Short, human-facing labels used for the title-row pill.
_PLATFORM_LABELS = {
    PLATFORM_YOUTUBE: "YT",
    PLATFORM_TIKTOK: "TikTok",
    PLATFORM_INSTAGRAM: "Insta",
    PLATFORM_FACEBOOK: "FB",
}

# Netscape-format cookie file names, keyed by platform. Single source of truth:
# the worker, the metadata extractor and the API layer all resolve their cookie
# paths from this map, so a new platform only has to be added here.
PLATFORM_COOKIE_FILENAMES: dict[str, str] = {
    PLATFORM_YOUTUBE: "youtube_cookies.txt",
    PLATFORM_INSTAGRAM: "instagram_cookies.txt",
    PLATFORM_TIKTOK: "tiktok_cookies.txt",
    PLATFORM_FACEBOOK: "facebook_cookies.txt",
}

_INSTAGRAM_EXACT_HOSTS = frozenset({"instagr.am", "www.instagr.am"})
_FACEBOOK_EXACT_HOSTS = frozenset({"fb.watch", "www.fb.watch", "fb.gg", "www.fb.gg"})
# ASCII digits only (\d also matches Unicode digits) and a non-empty username
# after "@" (plain .startswith("@") also accepts the bare "@" segment itself).
_TIKTOK_ID_RE = re.compile(r"^[0-9]{8,}$")
_TIKTOK_USERNAME_RE = re.compile(r"^@[A-Za-z0-9._]{1,24}$")
_TIKTOK_SHARE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Facebook numeric object IDs; share/reel codes are alphanumeric instead.
_FACEBOOK_ID_RE = re.compile(r"^[0-9]{6,}$")
_FACEBOOK_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FACEBOOK_PAGE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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
    if host == "facebook.com" or host.endswith(".facebook.com") or host in _FACEBOOK_EXACT_HOSTS:
        return PLATFORM_FACEBOOK

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
    if len(segments) == 3 and _TIKTOK_USERNAME_RE.fullmatch(segments[0]) and segments[1] in {"video", "photo"}:
        if _TIKTOK_ID_RE.fullmatch(segments[2]):
            return True, ""

    # Share hosts: vm.tiktok.com/<code>, vt.tiktok.com/<code>
    if host in {"vm.tiktok.com", "vt.tiktok.com"} and len(segments) == 1 and _TIKTOK_SHARE_CODE_RE.fullmatch(segments[0]):
        return True, ""

    # Redirect/share path: /t/<code>
    if host in {"tiktok.com", "www.tiktok.com"} and len(segments) == 2 and segments[0] == "t" and _TIKTOK_SHARE_CODE_RE.fullmatch(segments[1]):
        return True, ""

    return False, "Invalid TikTok URL. Expected a TikTok video, photo, or share link."


def _validate_instagram_url(url: str) -> tuple[bool, str]:
    """Light structural check for Instagram post/reel URLs."""
    parsed = urlparse(url)
    segments = [seg for seg in (parsed.path or "").strip("/").split("/") if seg]
    # Accept /p/<code>, /reel/<code>, /tv/<code>.
    if len(segments) == 2 and segments[0] in {"p", "reel", "tv"} and re.fullmatch(r"[A-Za-z0-9_-]+", segments[1]):
        return True, ""
    return False, "Invalid Instagram URL. Expected an instagram.com post, reel, or tv link."


def _validate_facebook_url(url: str) -> tuple[bool, str]:
    """Light structural check for Facebook video/reel/share URLs.

    Facebook's URL space is far less regular than TikTok's or Instagram's - the
    same video is reachable via /watch, a page permalink, a reel, a short
    fb.watch link and the newer /share/v/ form, and page slugs are effectively
    unconstrained. Validation therefore only rejects shapes that clearly are
    not a single video; yt-dlp performs the real resolution.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    segments = [seg for seg in (parsed.path or "").strip("/").split("/") if seg]
    error = "Invalid Facebook URL. Expected a Facebook video, reel, or share link."

    # Short share hosts: fb.watch/<code>, fb.gg/<code>
    if host in _FACEBOOK_EXACT_HOSTS:
        if len(segments) >= 1 and _FACEBOOK_CODE_RE.fullmatch(segments[0]):
            return True, ""
        return False, error

    if not segments:
        return False, error

    # /watch/?v=<id>, /watch?v=<id> and the legacy /video.php?v=<id>
    if segments[0] in {"watch", "video.php"}:
        video_id = (parse_qs(parsed.query, keep_blank_values=False).get("v") or [""])[0].strip()
        if _FACEBOOK_ID_RE.fullmatch(video_id):
            return True, ""
        return False, error

    # /reel/<id>
    if segments[0] == "reel" and len(segments) >= 2 and _FACEBOOK_CODE_RE.fullmatch(segments[1]):
        return True, ""

    # /share/v/<code>, /share/r/<code>
    if segments[0] == "share" and len(segments) >= 3 and segments[1] in {"v", "r"}:
        if _FACEBOOK_CODE_RE.fullmatch(segments[2]):
            return True, ""
        return False, error

    # Page permalinks: /<page>/videos/<id> and /<page>/videos/<slug>/<id>
    if len(segments) >= 3 and segments[1] == "videos" and _FACEBOOK_PAGE_RE.fullmatch(segments[0]):
        if _FACEBOOK_CODE_RE.fullmatch(segments[-1]):
            return True, ""

    return False, error


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
    if not value.lower().startswith("https://"):
        return False, "URL must start with https://"

    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False, "Invalid URL"

    # yt-dlp is handed platform cookie files for this URL, so the *submitted*
    # URL must use encrypted transport, carry no embedded credentials, and use
    # the default HTTPS port - this does not constrain redirect targets, which
    # would need outbound network-level enforcement to control.
    if parsed.username is not None or parsed.password is not None:
        return False, "URL must not contain credentials"
    if port not in (None, 443):
        return False, "URL must not use a custom port"

    platform = detect_platform(value)
    if platform == PLATFORM_YOUTUBE:
        return validate_youtube_url(value)
    if platform == PLATFORM_TIKTOK:
        return _validate_tiktok_url(value)
    if platform == PLATFORM_INSTAGRAM:
        return _validate_instagram_url(value)
    if platform == PLATFORM_FACEBOOK:
        return _validate_facebook_url(value)

    return False, "Unsupported URL. Supported platforms: YouTube, TikTok, Instagram, Facebook."
