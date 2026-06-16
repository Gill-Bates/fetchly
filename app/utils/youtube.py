#!/usr/bin/env python3
#
# app/utils/youtube.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""YouTube URL validation and metadata extraction utilities."""

import asyncio
import json
import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
import threading
from time import monotonic
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_COOKIES_DIR: Path = Path(__file__).parent.parent.parent
_COOKIES_DATA_DIR: Path = _COOKIES_DIR / "data"
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
_VIDEO_ID_RE = re.compile(r"[\w-]{11}")
_CACHE_TTL_SECONDS = 300
_INFO_CACHE_MAXSIZE = 256
_yt_dlp_local = threading.local()


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


# Backward-compatible alias for older imports.
_strip_zwsp = strip_zwsp


def _clean_video_id(value: str) -> str:
    """Strip zero-width chars and non-ID characters from video ID."""
    cleaned = strip_zwsp(value.strip())
    return re.sub(r"[^\w-]", "", cleaned)


def _is_video_id(value: str) -> bool:
    return bool(_VIDEO_ID_RE.fullmatch(value))


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


def _build_ydl_opts(url: str = "") -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
    }
    cookie_path = _resolve_cookie_path(url) if url else None
    if cookie_path is not None:
        opts["cookiefile"] = str(cookie_path)
    return opts


def _build_yt_dlp_cmd(url: str) -> list[str]:
    cmd = ["yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json"]
    cookie_path = _resolve_cookie_path(url)
    if cookie_path is not None:
        cmd.extend(["--cookies", str(cookie_path)])
    cmd.append("--")
    cmd.append(url)
    return cmd


def _cache_bucket() -> int:
    return int(monotonic() // _CACHE_TTL_SECONDS)


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
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "formats": formats,
        "unavailable": False,
    }


def _ydl_opts_signature(ydl_opts: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in ydl_opts.items()))


def _get_yt_dlp_instance(ydl_opts: dict[str, Any]) -> Any:
    import yt_dlp

    signature = _ydl_opts_signature(ydl_opts)
    cached_signature = getattr(_yt_dlp_local, "signature", None)
    cached_ydl = getattr(_yt_dlp_local, "ydl", None)
    if cached_ydl is None or cached_signature != signature:
        cached_ydl = yt_dlp.YoutubeDL(ydl_opts)
        _yt_dlp_local.signature = signature
        _yt_dlp_local.ydl = cached_ydl
    return cached_ydl


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
    
    # Must start with http:// or https://
    if not url.startswith(('http://', 'https://')):
        return False, "URL must start with http:// or https://"

    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "Invalid URL"

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    # Check if it's a YouTube video URL. Allow extra query parameters such as
    # playlist context (list/start_radio), but require a real video ID.
    if host == "youtu.be" or host == "www.youtu.be":
        segment = _clean_video_id(path.strip("/").split("/")[0])
        if _is_video_id(segment):
            return True, ""
    elif _is_youtube_host(host):
        segments = path.strip("/").split("/") if path.strip("/") else []
        if path == "/watch":
            params = parse_qs(parsed.query, keep_blank_values=False)
            video_id = _clean_video_id((params.get("v") or [""])[0])
            if _is_video_id(video_id):
                return True, ""
        elif segments and segments[0] in {"shorts", "embed", "v"}:
            segment = _clean_video_id(segments[1]) if len(segments) > 1 else ""
            if _is_video_id(segment):
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
    """Load video metadata from YouTube using yt-dlp.
    
    Tries the Python library first, falls back to subprocess.
    
    Args:
        url: YouTube video URL
        
    Returns:
        Video info dict or None if extraction fails
    """
    info: InfoPayload | None = None
    ydl_opts = _build_ydl_opts(url)
    
    try:
        from yt_dlp.utils import DownloadError, ExtractorError

        ydl = _get_yt_dlp_instance(ydl_opts)
        extracted = ydl.extract_info(url, download=False)
        if isinstance(extracted, dict):
            info = _prune_info(extracted)
    except ImportError:
        info = None
    except (DownloadError, ExtractorError, ValueError) as exc:
        logger.debug("yt-dlp library extraction failed for %s: %s", url, exc)
        info = None
    except OSError:
        logger.exception("System-level yt-dlp library error for %s", url)
        info = None

    if info is None:
        try:
            cmd = _build_yt_dlp_cmd(url)
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if not result.stdout:
                return None

            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                info = _prune_info(parsed)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp subprocess timed out for %s", url)
            return None
        except FileNotFoundError:
            logger.error("yt-dlp command not found in PATH")
            raise
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr[:500] if exc.stderr else None
            logger.debug(
                "yt-dlp subprocess extraction failed for %s: rc=%s stderr=%s",
                url,
                exc.returncode,
                stderr,
            )
            return None
        except json.JSONDecodeError as exc:
            logger.debug("yt-dlp subprocess returned invalid JSON for %s: %s", url, exc)
            return None
        except Exception:
            logger.exception("Unexpected yt-dlp subprocess error for %s", url)
            return None

    return info


@lru_cache(maxsize=_INFO_CACHE_MAXSIZE)
def _cached_load_video_info(normalized_url: str, cache_bucket: int) -> InfoPayload | None:
    _ = cache_bucket
    return _load_video_info_uncached(normalized_url)


def load_video_info(url: str) -> InfoPayload | None:
    """Load video metadata from YouTube using yt-dlp.
    
    This function is blocking; async callers should use
    :func:`load_video_info_async` or offload it via ``asyncio.to_thread()``.
    
    Args:
        url: YouTube video URL
        
    Returns:
        Video info dict or None if extraction fails
    """
    normalized_url = normalize_info_url(url)
    return _cached_load_video_info(normalized_url, _cache_bucket())


async def load_video_info_async(url: str) -> InfoPayload | None:
    """Async wrapper around :func:`load_video_info`."""
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
