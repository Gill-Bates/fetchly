#!/usr/bin/env python3
#
# app/utils/youtube.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""YouTube URL validation and metadata extraction utilities."""

import asyncio
import json
import logging
import math
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from .fs import get_data_dir

logger = logging.getLogger(__name__)

_COOKIES_DIR: Path = Path(__file__).parent.parent.parent
_COOKIES_DATA_DIR: Path = get_data_dir()
_PLATFORM_COOKIE_FILENAMES: dict[str, str] = {
    "youtube": "youtube_cookies.txt",
    "instagram": "instagram_cookies.txt",
    "tiktok": "tiktok_cookies.txt",
}
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_ZWSP_RE = re.compile(r"[\u200B-\u200D\uFEFF]")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}", re.ASCII)
_CACHE_TTL_SECONDS = 300
_INFO_CACHE_MAXSIZE = 256
_YOUTUBE_FALLBACK_PLAYER_CLIENT = "android"
_SUBPROCESS_TIMEOUT_SECONDS = 20

# Cap how many extractions can be in flight. subprocess.run()'s own timeout
# below is what actually bounds each one (see _load_video_info_uncached) - a
# yt_dlp.YoutubeDL().extract_info() call in-process was tried first here, but
# a genuinely hung extraction cannot be killed from the Python side: an
# asyncio.wait_for() around asyncio.to_thread() stops *awaiting* the result,
# it does not stop the underlying OS thread, which keeps running and holding
# a slot in the default thread pool shared by every asyncio.to_thread() call
# in the app (DB writes, housekeeping, ...) forever. A subprocess can actually
# be killed on timeout, so extraction goes through subprocess.run() only.
_METADATA_SLOTS = asyncio.Semaphore(4)


class InfoPayload(TypedDict):
    title: str | None
    channel: str | None
    uploader: str | None
    duration: int | None
    view_count: int | None
    thumbnail: str | None
    formats: list[dict[str, Any]]
    unavailable: bool


def strip_zwsp(text: str) -> str:
    return _ZWSP_RE.sub("", text)


def _is_video_id(value: str) -> bool:
    """Return True if *value* is exactly a YouTube video ID.

    Only zero-width characters and surrounding whitespace are stripped; the ID
    is validated, never repaired. Silently deleting invalid characters would
    turn a malformed ID into an accepted one while the rest of the pipeline
    keeps working with the original, unrepaired string.
    """
    return _VIDEO_ID_RE.fullmatch(strip_zwsp(value).strip()) is not None


def _is_youtube_host(host: str) -> bool:
    return host in _YOUTUBE_HOSTS


def _resolve_cookie_path(url: str) -> Path | None:
    """Return the platform-specific cookie file for a URL, if it exists."""
    from .platform import detect_platform

    platform = detect_platform(url)
    if not platform:
        return None

    filename = _PLATFORM_COOKIE_FILENAMES.get(platform)
    if not filename:
        return None

    custom_dir = Path(custom_dir_raw) if (custom_dir_raw := os.environ.get("TUBEYOU_COOKIES_DIR", "").strip()) else None
    search_dirs = [custom_dir, _COOKIES_DIR, _COOKIES_DATA_DIR]
    for directory in search_dirs:
        if directory is None:
            continue
        path = directory / filename
        if path.is_file():
            return path
    return None


def _url_for_log(url: str) -> str:
    """Strip query and fragment before a URL reaches the log.

    TikTok/Instagram share links commonly carry tracking or signed query
    parameters that do not belong in operational logs.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<unparsable URL>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _build_yt_dlp_cmd(url: str, *, player_client: str | None = None) -> list[str]:
    cmd = ["yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json"]
    if player_client is not None:
        cmd.extend(["--extractor-args", f"youtube:player_client={player_client}"])
    cookie_path = _resolve_cookie_path(url)
    if cookie_path is not None:
        cmd.extend(["--cookies", str(cookie_path)])
    cmd.append("--")
    cmd.append(url)
    return cmd


def _cache_bucket() -> int:
    return int(monotonic() // _CACHE_TTL_SECONDS)


def _non_negative_int(value: object) -> int | None:
    """Coerce a yt-dlp numeric field to InfoPayload's declared int type.

    yt-dlp commonly returns duration/view_count as float (or omits them, or -
    for some extractors - returns a negative placeholder); InfoPayload
    promises int | None, and callers rely on that: extract_video_meta() only
    renders "Duration: ..." behind isinstance(duration, int), so a float here
    silently drops the line instead of raising anything.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def _prune_info(info: dict[str, Any] | None) -> InfoPayload | None:
    """Keep only the metadata fields the API actually uses before caching."""
    if not isinstance(info, dict):
        return None

    raw_formats = info.get("formats")
    formats: list[dict[str, Any]] = []
    if isinstance(raw_formats, list):
        for fmt in raw_formats:
            if not isinstance(fmt, dict):
                continue
            formats.append(
                {
                    "format_id": fmt.get("format_id"),
                    "ext": fmt.get("ext"),
                    "vcodec": fmt.get("vcodec"),
                    "acodec": fmt.get("acodec"),
                    "height": fmt.get("height"),
                    "abr": fmt.get("abr"),
                    "filesize": fmt.get("filesize"),
                }
            )

    return {
        "title": info.get("title"),
        "channel": info.get("channel"),
        "uploader": info.get("uploader"),
        "duration": _non_negative_int(info.get("duration")),
        "view_count": _non_negative_int(info.get("view_count")),
        "thumbnail": info.get("thumbnail"),
        "formats": formats,
        "unavailable": False,
    }


def validate_youtube_url(url: str) -> tuple[bool, str]:
    """
    Validate that a URL is a valid YouTube video URL.
    
    Args:
        url: The URL to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not url or not isinstance(url, str):
        return False, "URL is required"
    
    url = url.strip()
    
    # Normalize common copy/paste issues from mobile apps / HTML sources
    url = strip_zwsp(url.replace("&amp;", "&"))
    
    if not url:
        return False, "URL is required"
    
    # Check URL length (prevent DoS with extremely long URLs)
    if len(url) > 2048:
        return False, "URL is too long"
    
    # Metadata requests must use encrypted transport.
    if not url.startswith("https://"):
        return False, "URL must start with https://"

    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "Invalid URL"

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    # Check if it's a YouTube video URL. Allow extra query parameters such as
    # playlist context (list/start_radio), but require a real video ID.
    if host == "youtu.be" or host == "www.youtu.be":
        segment = path.strip("/").split("/")[0]
        if len(path.strip("/").split("/")) == 1 and _is_video_id(segment):
            return True, ""
    elif _is_youtube_host(host):
        segments = path.strip("/").split("/") if path.strip("/") else []
        if path == "/watch":
            params = parse_qs(parsed.query, keep_blank_values=False)
            video_id = (params.get("v") or [""])[0]
            if _is_video_id(video_id):
                return True, ""
        elif segments and segments[0] in {"shorts", "embed", "v"}:
            segment = segments[1] if len(segments) > 1 else ""
            if len(segments) == 2 and _is_video_id(segment):
                return True, ""

    return False, "Invalid YouTube URL. Supported formats: youtube.com/watch?v=..., youtu.be/..., youtube.com/shorts/..."


def normalize_info_url(url: str) -> str:
    """Normalize a YouTube URL to a single-video URL for metadata extraction.

    This removes playlist context (e.g. list/index) that can trigger slower
    resolution paths and intermittent timeouts in yt-dlp. Callers should
    validate the input URL first.
    """
    value = strip_zwsp(url.strip().replace("&amp;", "&"))
    try:
        parsed = urlparse(value)
    except ValueError:
        return value

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    segments = path.strip("/").split("/") if path.strip("/") else []

    if _is_youtube_host(host) and path == "/watch":
        params = parse_qs(parsed.query, keep_blank_values=False)
        video_id = (params.get("v") or [""])[0].strip()
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return value

    if host == "youtu.be" or host == "www.youtu.be":
        segment = path.strip("/").split("/")[0].strip()
        if segment:
            return f"https://www.youtube.com/watch?v={segment}"

    if _is_youtube_host(host) and segments and segments[0] in {"shorts", "embed", "v"}:
        segment = segments[1].strip() if len(segments) > 1 else ""
        if segment:
            return f"https://www.youtube.com/watch?v={segment}"

    return value


def _load_video_info_uncached(url: str) -> InfoPayload | None:
    """Load video metadata for a URL by shelling out to yt-dlp.

    Best-effort: every failure mode - timeout, missing binary, bad JSON,
    non-zero exit - is swallowed and reported as None so callers degrade
    gracefully instead of surfacing a server error for what is, from the
    caller's perspective, just "no metadata available yet".

    Args:
        url: YouTube video URL

    Returns:
        Video info dict or None if extraction fails
    """
    from .platform import detect_platform

    player_clients: tuple[str | None, ...]
    if detect_platform(url) == "youtube":
        # Older yt-dlp releases can fail the default web client with
        # "The page needs to be reloaded". Android still provides metadata in
        # that case; newer releases continue to use the default client first.
        player_clients = (None, _YOUTUBE_FALLBACK_PLAYER_CLIENT)
    else:
        player_clients = (None,)

    for player_client in player_clients:
        info: InfoPayload | None = None

        try:
            cmd = _build_yt_dlp_cmd(url, player_client=player_client)
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            if result.stdout:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    info = _prune_info(parsed)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp subprocess timed out for %s", _url_for_log(url))
        except FileNotFoundError:
            # main.py's startup check refuses to run without yt-dlp on PATH,
            # so this should be unreachable in a running instance - but this
            # function's contract (like every sibling except-branch here) is
            # to degrade to None, not to raise past a best-effort caller.
            logger.error("yt-dlp command not found in PATH")
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr[:500] if exc.stderr else None
            logger.debug(
                "yt-dlp subprocess extraction failed for %s: rc=%s stderr=%s",
                _url_for_log(url),
                exc.returncode,
                stderr,
            )
        except json.JSONDecodeError as exc:
            logger.debug("yt-dlp subprocess returned invalid JSON for %s: %s", _url_for_log(url), exc)
        except (OSError, UnicodeError) as exc:
            logger.warning("yt-dlp subprocess failed for %s: %s", _url_for_log(url), exc)

        if info is not None:
            return info

        if player_client is None and len(player_clients) > 1:
            logger.debug("Retrying YouTube metadata extraction with %s client", _YOUTUBE_FALLBACK_PLAYER_CLIENT)

    return None


@lru_cache(maxsize=_INFO_CACHE_MAXSIZE)
def _cached_load_video_info(normalized_url: str, cache_bucket: int) -> InfoPayload | None:
    _ = cache_bucket
    return _load_video_info_uncached(normalized_url)


def load_video_info(url: str) -> InfoPayload | None:
    """Load video metadata for a supported platform URL using yt-dlp.

    The allowlist is enforced here as well as in the routes: this is the point
    where yt-dlp performs the outbound request, so an unvalidated URL must
    never reach it.

    This function is blocking; async callers should use
    :func:`load_video_info_async` or offload it via ``asyncio.to_thread()``.

    Args:
        url: Media URL of a supported platform

    Returns:
        Video info dict or None if the URL is rejected or extraction fails
    """
    from .platform import validate_media_url

    is_valid, error = validate_media_url(url)
    if not is_valid:
        logger.error("Refusing metadata extraction for unsupported URL: %s", error)
        return None

    normalized_url = normalize_info_url(url)
    return _cached_load_video_info(normalized_url, _cache_bucket())


async def load_video_info_async(url: str) -> InfoPayload | None:
    """Async wrapper around :func:`load_video_info`."""
    async with _METADATA_SLOTS:
        return await asyncio.to_thread(load_video_info, url)


def extract_video_meta(url: str) -> dict[str, object]:
    """Best-effort metadata extraction for title + hover details.
    
    Args:
        url: YouTube video URL
        
    Returns:
        Dict with video_title and video_meta_hover keys
    """
    info = load_video_info(url)
    if info is None:
        return {"video_title": None, "video_meta_hover": None}

    title = str(info.get("title") or "").strip()
    channel = str(info.get("channel") or "").strip()
    uploader = str(info.get("uploader") or "").strip()
    duration = info.get("duration")
    views = info.get("view_count")

    lines: list[str] = []
    if channel:
        lines.append(f"Channel: {channel}")
    if uploader:
        lines.append(f"Uploader: {uploader}")
    if isinstance(duration, int) and duration > 0:
        hours, remainder = divmod(duration, 3600)
        mins, secs = divmod(remainder, 60)
        if hours:
            lines.append(f"Duration: {hours}:{mins:02d}:{secs:02d}")
        else:
            lines.append(f"Duration: {mins}:{secs:02d}")
    if isinstance(views, int) and views >= 0:
        lines.append(f"Views: {views:,}")

    return {
        "video_title": title or None,
        "video_meta_hover": " | ".join(lines) if lines else None,
    }


async def extract_video_meta_async(url: str) -> dict[str, object]:
    """Async wrapper around :func:`extract_video_meta`."""
    async with _METADATA_SLOTS:
        return await asyncio.to_thread(extract_video_meta, url)


def empty_info_payload() -> InfoPayload:
    """Return the canonical fallback payload when video metadata is unavailable."""
    return {
        "title": None,
        "channel": None,
        "uploader": None,
        "duration": None,
        "view_count": None,
        "thumbnail": None,
        "formats": [],
        "unavailable": True,
    }
