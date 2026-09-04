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
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from .cookie_status import cookie_file_is_usable
from .cookies import find_cookie_file

logger = logging.getLogger(__name__)

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

# Cap in-flight extractions. Extraction shells out to yt-dlp rather than
# calling it in-process because a hung in-process call cannot be killed - it
# would hold a slot in the shared asyncio thread pool forever; a subprocess
# can be killed on timeout.
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

    Only zero-width chars and whitespace are stripped; the ID is validated,
    never repaired (a repaired ID would diverge from the string the rest of
    the pipeline still carries).
    """
    return _VIDEO_ID_RE.fullmatch(strip_zwsp(value).strip()) is not None


def _is_youtube_host(host: str) -> bool:
    return host in _YOUTUBE_HOSTS


def _resolve_cookie_path(url: str) -> Path | None:
    """The platform's cookie file for a URL, if it exists and is usable.

    An expired jar is skipped (yt-dlp would still send it, and a stale login is
    answered worse than an anonymous probe) - same rule as app/worker.py.
    """
    # Lazy import: platform.py imports this module, so this would close the cycle.
    from .platform import PLATFORM_COOKIE_FILENAMES, detect_platform

    platform = detect_platform(url)
    if not platform:
        return None

    filename = PLATFORM_COOKIE_FILENAMES.get(platform)
    if not filename:
        return None

    cookie_path = find_cookie_file(filename)
    if cookie_path is None or not cookie_file_is_usable(cookie_path, platform):
        return None
    return cookie_path


def _url_for_log(url: str) -> str:
    """Strip query and fragment before a URL is logged (share links carry
    tracking and signed params).
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
    """Coerce a yt-dlp numeric field (often float, or a negative placeholder)
    to InfoPayload's ``int | None`` - callers gate on ``isinstance(x, int)``.
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
    """Validate that a URL is a YouTube video URL. Returns ``(is_valid, error)``."""
    if not url or not isinstance(url, str):
        return False, "URL is required"

    url = url.strip()
    url = strip_zwsp(url.replace("&amp;", "&"))  # common copy/paste damage

    if not url:
        return False, "URL is required"

    if len(url) > 2048:
        return False, "URL is too long"

    # Case-insensitive to agree with platform.validate_media_url() (the gate
    # this sits behind); urlparse() lowercases scheme and host anyway.
    if not url.lower().startswith("https://"):
        return False, "URL must start with https://"

    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "Invalid URL"

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    # Check if it's a YouTube video URL. Allow extra query parameters such as
    # playlist context (list/start_radio), but require a real video ID.
    if host in {"youtu.be", "www.youtu.be"}:
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

    return False, (
        "Invalid YouTube URL. Supported formats: "
        "youtube.com/watch?v=..., youtu.be/..., youtube.com/shorts/..."
    )


def normalize_info_url(url: str) -> str:
    """Reduce a YouTube URL to ``watch?v=<id>``, dropping playlist context
    that makes yt-dlp slower and flakier. Validate the URL first.
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

    if host in {"youtu.be", "www.youtu.be"}:
        segment = path.strip("/").split("/")[0].strip()
        if segment:
            return f"https://www.youtube.com/watch?v={segment}"

    if _is_youtube_host(host) and segments and segments[0] in {"shorts", "embed", "v"}:
        segment = segments[1].strip() if len(segments) > 1 else ""
        if segment:
            return f"https://www.youtube.com/watch?v={segment}"

    return value


def _load_video_info_uncached(url: str) -> InfoPayload | None:
    """Load video metadata by shelling out to yt-dlp.

    Best-effort: every failure mode is swallowed and reported as None.
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
                # yt-dlp emits UTF-8 JSON regardless of the container locale;
                # pin the decode so a title in a non-Latin script survives even
                # when LANG is unset and Python's UTF-8 mode is not in effect.
                encoding="utf-8",
                errors="replace",
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            if result.stdout:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    info = _prune_info(parsed)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp subprocess timed out for %s", _url_for_log(url))
        except FileNotFoundError:
            # Startup checks for yt-dlp, so this should be unreachable; degrade
            # to None rather than raise past a best-effort caller.
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

    Re-enforces the platform allowlist here, since this is where the outbound
    request happens. Blocking; async callers use :func:`load_video_info_async`.
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
    """Best-effort metadata for title + hover details.

    Returns ``video_title``, ``video_meta_hover``, ``duration_seconds`` (the
    source's own runtime, shown until ffprobe measures the finished file).
    """
    info = load_video_info(url)
    if info is None:
        return {"video_title": None, "video_meta_hover": None, "duration_seconds": None}

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
        # load_video_info() normalizes this to a non-negative int or None.
        "duration_seconds": duration if isinstance(duration, int) and duration > 0 else None,
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
