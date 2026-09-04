#!/usr/bin/env python3
#
# app/utils/updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Upstream release checks for the tools shown on the settings page.

Each component is compared against the source the image actually installs it
from — GitHub releases for the packages, the rolling BtbN build release for
ffmpeg. The answer is cached for 24 hours (in memory plus a small JSON file in
the data directory) so a page reload never triggers another network round trip.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx

from .fs import get_data_dir

logger = logging.getLogger(__name__)

CACHE_FILENAME = "update_check.json"
# Bumped on layout change; mismatched files are discarded, not migrated.
CACHE_SCHEMA = 4
CACHE_TTL_SECONDS = 24 * 60 * 60  # after a successful check
RETRY_TTL_SECONDS = 60 * 60       # after a failed one
# Hard ceiling on the whole concurrent fetch-all round. httpx's per-phase
# timeout does not bound a redirect chain, so this is the real deadline every
# /api/updates request can wait on via _lock.
_TOTAL_FETCH_TIMEOUT = 20.0

_REQUEST_TIMEOUT = 6.0
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "fetchly-update-check",
}

# Rolling release the ffmpeg build stage pulls from (see docker/Dockerfile).
FFMPEG_BUILDS_REPO = "BtbN/FFmpeg-Builds"
FFMPEG_CHECKSUMS_URL = f"https://github.com/{FFMPEG_BUILDS_REPO}/releases/download/latest/checksums.sha256"
# Stable BtbN asset, e.g. "ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz". The
# backreference keeps out the "master" nightlies of ffmpeg's dev branch.
_FFMPEG_ASSET_RE = re.compile(
    r"^ffmpeg-n(\d+(?:\.\d+)*)-latest-linux(?:64|arm64)-(?:gpl|lgpl)-\1\.tar\.xz$"
)
# Leading numeric run of a version string ("n9.0.1-4-gabc" -> "9.0.1").
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")

UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _Component:
    """A tool whose installed version is compared against its upstream source."""

    label: str
    repo: str
    # releases: GitHub releases API. tags: Git tags API. ffmpeg-builds: the
    # rolling BtbN release's asset list.
    source: Literal["releases", "tags", "ffmpeg-builds"]


COMPONENTS: dict[str, _Component] = {
    "fetchly": _Component(label="fetchly", repo="Gill-Bates/fetchly", source="tags"),
    "ytdlp": _Component(label="yt-dlp", repo="yt-dlp/yt-dlp", source="releases"),
    "ytdlp_ejs": _Component(label="yt-dlp-ejs", repo="yt-dlp/ejs", source="releases"),
    "js_runtime": _Component(label="deno", repo="denoland/deno", source="releases"),
    "ffmpeg": _Component(label="ffmpeg", repo=FFMPEG_BUILDS_REPO, source="ffmpeg-builds"),
    "wavesurfer": _Component(label="wavesurfer.js", repo="katspaugh/wavesurfer.js", source="releases"),
}

_lock = asyncio.Lock()
_cache: dict[str, Any] | None = None


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    """Return the leading numeric components of *value* as a comparable tuple."""
    if not value:
        return None
    match = _VERSION_RE.search(value.strip().lstrip("vVnN"))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _is_prerelease_tag(tag: str) -> bool:
    """True for a prerelease marker like '8.0.0-beta.3'.

    Narrow on purpose: build metadata ("n9.0-4-gabc1234", "7.1.5-0+deb13u1")
    is not a prerelease. Only "-"/"_"/"." + an alpha/beta/rc/... token count.
    """
    return bool(re.search(r"[-_.](?:alpha|beta|rc|dev|pre|a|b)\d*(?:[-+_.]|$)", tag, re.IGNORECASE))


def _is_newer(latest: str | None, current: str | None) -> bool:
    """True when *latest* is a strictly higher version than *current*."""
    latest_parts = _parse_version(latest)
    current_parts = _parse_version(current)
    if not latest_parts or not current_parts:
        return False

    length = max(len(latest_parts), len(current_parts))
    padded_latest = latest_parts + (0,) * (length - len(latest_parts))
    padded_current = current_parts + (0,) * (length - len(current_parts))
    if padded_latest != padded_current:
        return padded_latest > padded_current

    # Same numbers: a stable release beats a prerelease of itself, but build
    # metadata does not ("n9.0" must not beat "n9.0-4-gabc1234").
    return _is_prerelease_tag(current or "") and not _is_prerelease_tag(latest or "")


# Cap pages per component so a release-happy repo cannot blow the fetch
# budget: 3 x 50 releases / 3 x 100 tags is more than any configured repo has.
_MAX_RELEASE_PAGES = 3
# Bound an upstream response's memory (lists are normally a few KiB).
_MAX_API_RESPONSE_BYTES = 1024 * 1024
_MAX_FFMPEG_CHECKSUM_BYTES = 1024 * 1024


async def _read_response_bytes(
    client: httpx.AsyncClient, url: str, *, max_bytes: int
) -> tuple[httpx.Response, bytes]:
    """Stream one response, rejecting it before it can exceed *max_bytes*."""
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > max_bytes:
                raise ValueError(f"upstream response exceeds {max_bytes} byte limit")

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise ValueError(f"upstream response exceeds {max_bytes} byte limit")
            body.extend(chunk)
        return response, bytes(body)


async def _fetch_latest_release(client: httpx.AsyncClient, repo: str) -> str | None:
    """Return the highest published, non-prerelease release tag of *repo*.

    Not ``/releases/latest``: that honours only the prerelease flag, which
    some repos leave unset on betas. Filters on flag *and* tag name, takes the
    highest version (not the most recent), bounded to _MAX_RELEASE_PAGES.
    """
    best_tag: str | None = None
    best_parts: tuple[int, ...] = ()
    url: str | None = f"https://api.github.com/repos/{repo}/releases?per_page=50"

    for _ in range(_MAX_RELEASE_PAGES):
        if url is None:
            break
        response, body = await _read_response_bytes(
            client, url, max_bytes=_MAX_API_RESPONSE_BYTES
        )
        payload = json.loads(body)
        if not isinstance(payload, list):
            break

        for release in payload:
            if not isinstance(release, dict):
                continue
            if release.get("draft") or release.get("prerelease"):
                continue
            tag = str(release.get("tag_name") or "").strip()
            if not tag or _is_prerelease_tag(tag):
                continue
            parts = _parse_version(tag)
            if parts and parts > best_parts:
                best_tag, best_parts = tag, parts

        url = response.links.get("next", {}).get("url")

    return best_tag


async def _fetch_latest_tag(client: httpx.AsyncClient, repo: str) -> str | None:
    """Return the highest stable version tag of a repo.

    fetchly ships from Git tags (a GitHub Release may lag or never appear), so
    the tag list is the authoritative self-update source.
    """
    best_tag: str | None = None
    best_parts: tuple[int, ...] = ()
    url: str | None = f"https://api.github.com/repos/{repo}/tags?per_page=100"

    for _ in range(_MAX_RELEASE_PAGES):
        if url is None:
            break
        response, body = await _read_response_bytes(
            client, url, max_bytes=_MAX_API_RESPONSE_BYTES
        )
        payload = json.loads(body)
        if not isinstance(payload, list):
            break

        for entry in payload:
            if not isinstance(entry, dict):
                continue
            tag = str(entry.get("name") or "").strip()
            if not tag or _is_prerelease_tag(tag):
                continue
            parts = _parse_version(tag)
            if parts and parts > best_parts:
                best_tag, best_parts = tag, parts

        url = response.links.get("next", {}).get("url")

    return best_tag


async def _fetch_latest_ffmpeg_build(client: httpx.AsyncClient) -> str | None:
    """Return the newest stable ffmpeg series offered by the BtbN builds.

    Matches what docker/Dockerfile installs when FFMPEG_SERIES is unset. If a
    deployment pins an older series, this still reports the newest - there is
    no build-time record of the pin to read.
    """
    _, body = await _read_response_bytes(
        client, FFMPEG_CHECKSUMS_URL, max_bytes=_MAX_FFMPEG_CHECKSUM_BYTES
    )

    best_series: str | None = None
    best_parts: tuple[int, ...] = ()
    for line in body.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        match = _FFMPEG_ASSET_RE.match(parts[1])
        if not match:
            continue
        series = match.group(1)
        version_parts = _parse_version(series)
        if version_parts and version_parts > best_parts:
            best_series, best_parts = series, version_parts

    return f"n{best_series}" if best_series else None


async def _fetch_component(
    client: httpx.AsyncClient, key: str, component: _Component
) -> tuple[str, str | None, bool]:
    """Fetch the newest upstream version of one component.

    Returns ``(key, tag, retry)``. ``retry`` is True only for transient
    failures (blip, timeout, rate limit) - a 404 is False, so a still-private
    repo does not drag the whole batch onto the short RETRY_TTL cadence.
    """
    try:
        if component.source == "ffmpeg-builds":
            tag = await _fetch_latest_ffmpeg_build(client)
        elif component.source == "tags":
            tag = await _fetch_latest_tag(client, component.repo)
        else:
            tag = await _fetch_latest_release(client, component.repo)
    except httpx.HTTPStatusError as exc:
        # 404 = not reachable (private/renamed/removed); anything else (403, 5xx)
        # is transient and worth a sooner retry.
        retry = exc.response.status_code != 404
        logger.log(
            logging.WARNING if retry else logging.INFO,
            "Update check for %s failed: %s",
            component.repo,
            exc,
        )
        return key, None, retry
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Update check for %s failed: %s", component.repo, exc)
        return key, None, True
    return key, tag, False


async def _fetch_all(
    previous: dict[str, dict[str, Any]], attempted_at: float
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Query every upstream source once. Returns ``(versions, all_succeeded)``.

    A transient failure keeps the component's last known version (with its own
    older ``checked_at``) and marks the round incomplete. A 404 drops any
    stale entry without holding back the round. Unknown components are dropped.
    """
    async with httpx.AsyncClient(
        timeout=_REQUEST_TIMEOUT,
        follow_redirects=True,
        max_redirects=3,
        headers=_HEADERS,
    ) as client:
        results = await asyncio.gather(
            *(_fetch_component(client, key, component) for key, component in COMPONENTS.items())
        )

    versions = {key: entry for key, entry in previous.items() if key in COMPONENTS}
    complete = True
    for key, tag, retry in results:
        if tag:
            versions[key] = {"version": tag, "checked_at": attempted_at}
        elif retry:
            complete = False
        else:
            # Resolved with nothing (e.g. still-private repo): drop stale entry.
            versions.pop(key, None)
    return versions, complete


def _cache_path() -> Path:
    return get_data_dir() / CACHE_FILENAME


def _parse_timestamp(value: Any) -> float:
    """Coerce a cached timestamp to a finite non-negative float; 0.0 otherwise
    (a hand-edited "Infinity" would else never expire).
    """
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(timestamp) or timestamp < 0.0:
        return 0.0
    return timestamp


_MAX_CACHE_BYTES = 64 * 1024


def _read_cache_file() -> dict[str, Any] | None:
    """Load the on-disk cache; None when missing, stale-schema, or unusable."""
    path = _cache_path()
    try:
        # Bounded read (stat + one extra byte) so a concurrent external writer
        # cannot turn this best-effort cache into an unbounded allocation.
        if path.stat().st_size > _MAX_CACHE_BYTES:
            logger.debug("Discarding oversized update cache at %s", path)
            return None
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CACHE_BYTES + 1)
        if len(raw) > _MAX_CACHE_BYTES:
            logger.debug("Discarding oversized update cache at %s", path)
            return None
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError.
        logger.debug("Discarding unusable update cache at %s", path)
        return None

    if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
        return None

    raw_versions = payload.get("versions")
    if not isinstance(raw_versions, dict):
        return None

    versions: dict[str, dict[str, Any]] = {}
    for key, entry in raw_versions.items():
        if key not in COMPONENTS or not isinstance(entry, dict):
            continue
        version = str(entry.get("version") or "").strip()
        if version:
            versions[str(key)] = {
                "version": version,
                "checked_at": _parse_timestamp(entry.get("checked_at")),
            }

    # Clamp a corrupted far-future expires_at to what a real write could produce,
    # or update checks would be disabled indefinitely.
    max_expiry = time.time() + CACHE_TTL_SECONDS
    expires_at = _parse_timestamp(payload.get("expires_at"))
    if expires_at > max_expiry:
        expires_at = 0.0

    return {
        "schema": CACHE_SCHEMA,
        "checked_at": _parse_timestamp(payload.get("checked_at")),
        "last_attempt_at": _parse_timestamp(payload.get("last_attempt_at")),
        "expires_at": expires_at,
        "complete": bool(payload.get("complete")),
        "versions": versions,
    }


def _write_cache_file(entry: dict[str, Any]) -> None:
    """Persist the cache (temp file + atomic rename) so restarts keep the 24h
    window. Single-process (WORKERS=1), so no cross-process lock.
    """
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle)
            Path(temp_name).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(temp_name).unlink()
            raise
    except OSError as exc:
        logger.debug("Unable to persist update cache to %s: %s", path, exc)


async def _get_latest_versions(force: bool = False) -> dict[str, Any]:
    """Return cached upstream versions, refreshing them when the cache expired."""
    global _cache

    now = time.time()
    if not force and _cache and _cache.get("expires_at", 0.0) > now:
        return _cache

    async with _lock:
        now = time.time()
        if not force and _cache and _cache.get("expires_at", 0.0) > now:
            return _cache

        if _cache is None:
            _cache = await asyncio.to_thread(_read_cache_file)
        if not force and _cache and _cache.get("expires_at", 0.0) > now:
            return _cache

        previous: dict[str, dict[str, Any]] = dict(_cache.get("versions", {})) if _cache else {}
        previous_checked_at = float(_cache.get("checked_at", 0.0)) if _cache else 0.0

        attempted_at = time.time()
        try:
            async with asyncio.timeout(_TOTAL_FETCH_TIMEOUT):
                versions, complete = await _fetch_all(previous, attempted_at)
        except TimeoutError:
            logger.warning("Upstream update check exceeded its %.0fs deadline", _TOTAL_FETCH_TIMEOUT)
            versions, complete = previous, False

        entry = {
            "schema": CACHE_SCHEMA,
            # Only a fully successful round advances "checked_at".
            "checked_at": attempted_at if complete else previous_checked_at,
            "last_attempt_at": attempted_at,
            "complete": complete,
            "expires_at": time.time() + (CACHE_TTL_SECONDS if complete else RETRY_TTL_SECONDS),
            "versions": versions,
        }
        _cache = entry
        await asyncio.to_thread(_write_cache_file, entry)
        return entry


def _release_url(component: _Component, tag: str | None) -> str:
    """Link to the release page for *tag* (ffmpeg always points at its rolling
    "latest" release, which has no per-version page).
    """
    if component.source == "ffmpeg-builds":
        return f"https://github.com/{component.repo}/releases/tag/latest"
    if component.source == "tags" and tag:
        return f"https://github.com/{component.repo}/tree/{quote(tag, safe='')}"
    if tag:
        # Tag names may contain path characters ("release/8.0").
        return f"https://github.com/{component.repo}/releases/tag/{quote(tag, safe='')}"
    return f"https://github.com/{component.repo}/releases/latest"


async def get_update_status(current: dict[str, str]) -> dict[str, Any]:
    """Compare the installed versions in *current* against the newest releases.

    ``checked_at`` is the last time every source answered; ``last_attempt_at``
    is the last time one was tried. A component whose own lookup failed in that
    attempt is served from the previous answer and marked ``stale``.
    """
    entry = await _get_latest_versions()
    latest_versions: dict[str, dict[str, Any]] = entry.get("versions", {})
    last_attempt_at = float(entry.get("last_attempt_at", 0.0))

    components: dict[str, Any] = {}
    for key, component in COMPONENTS.items():
        installed = str(current.get(key) or "").strip()
        if installed == UNAVAILABLE:
            installed = ""
        cached = latest_versions.get(key) or {}
        latest = str(cached.get("version") or "") or None
        checked_at = float(cached.get("checked_at", 0.0))
        components[key] = {
            "label": component.label,
            "current": installed,
            "latest": latest or "",
            "update_available": _is_newer(latest, installed),
            "url": _release_url(component, latest),
            "checked_at": checked_at,
            "stale": bool(latest) and checked_at < last_attempt_at,
        }

    return {
        "checked_at": float(entry.get("checked_at", 0.0)),
        "last_attempt_at": last_attempt_at,
        "complete": bool(entry.get("complete")),
        "components": components,
    }
