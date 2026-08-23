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
# Bumped when the cache layout changes; files written by another layout are
# discarded rather than migrated - it is a cache, refetching costs one request.
CACHE_SCHEMA = 2
# Successful check: do not ask GitHub again for 24 hours.
CACHE_TTL_SECONDS = 24 * 60 * 60
# Failed check: retry sooner, but never on every page load.
RETRY_TTL_SECONDS = 60 * 60
# httpx's own timeout bounds each connect/read/write/pool phase separately,
# not the request as a whole - with redirects (max_redirects=3) each hop gets
# its own budget, so one slow upstream could otherwise stretch well past
# _REQUEST_TIMEOUT. This is the hard ceiling on the whole fetch-all round
# (all 5 components, run concurrently), which every /api/updates request can
# end up waiting on via _lock while a refresh is in flight.
_TOTAL_FETCH_TIMEOUT = 20.0

_REQUEST_TIMEOUT = 6.0
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "tubeyou-update-check",
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
    # "releases" uses the GitHub releases API; "ffmpeg-builds" reads the asset
    # list of the rolling BtbN release the image is actually built from.
    source: Literal["releases", "ffmpeg-builds"]


COMPONENTS: dict[str, _Component] = {
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
    """True for versions carrying a prerelease marker such as '8.0.0-beta.3'.

    Deliberately narrow: build metadata like ffmpeg's git-describe suffix
    ("n9.0-4-gabc1234") or a Debian revision ("7.1.5-0+deb13u1") is not a
    prerelease and must not be treated as one.
    """
    # "-" leads a prerelease per SemVer; "+" leads build metadata, which does
    # NOT make a version a prerelease ("1.2.3+deb13u1" == "1.2.3" in precedence)
    # - so only "-", "_", "." trigger here, matching the docstring above.
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

    # Same numbers: the stable release supersedes a prerelease of itself
    # ("8.0.0" beats "8.0.0-rc1"), while build metadata does not make the
    # installed build outdated ("n9.0" must not beat "n9.0-4-gabc1234").
    return _is_prerelease_tag(current or "") and not _is_prerelease_tag(latest or "")


# Hard cap on pages walked per component so one release-happy repo cannot
# blow out the total request budget in _get_latest_versions (see the
# asyncio.timeout there). 3 pages * 50 = 150 releases, comfortably more than
# any of the five configured repos has ever published.
_MAX_RELEASE_PAGES = 3


async def _fetch_latest_release(client: httpx.AsyncClient, repo: str) -> str | None:
    """Return the highest published, non-prerelease release tag of *repo*.

    Not ``/releases/latest``: that endpoint honours only the repository's
    prerelease flag, and katspaugh/wavesurfer.js publishes betas (currently
    "8.0.0-beta.3") without setting it - so it would hand out a beta as the
    version to compare against. The list is filtered on both the flag and the
    tag name, and the highest version wins rather than the most recently
    published one, so a hotfix released later on an older branch cannot
    masquerade as the newest version. Bounded to _MAX_RELEASE_PAGES pages: a
    real "highest ever published" would mean following GitHub's Link-header
    pagination to the end, which is unbounded for a repo with many releases.
    """
    best_tag: str | None = None
    best_parts: tuple[int, ...] = ()
    url: str | None = f"https://api.github.com/repos/{repo}/releases?per_page=50"

    for _ in range(_MAX_RELEASE_PAGES):
        if url is None:
            break
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
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


async def _fetch_latest_ffmpeg_build(client: httpx.AsyncClient) -> str | None:
    """Return the newest stable ffmpeg series offered by the BtbN builds.

    The image does not use the distro package: docker/Dockerfile pulls a static
    upstream build from the rolling BtbN "latest" release and, when
    FFMPEG_SERIES is left unset, picks the highest stable ``nX.Y`` series
    present - which is exactly what this returns. If a deployment pins
    FFMPEG_SERIES to an older line, this still reports the newest series BtbN
    publishes, not the one an unmodified rebuild would actually produce; there
    is no build-time record of that pin for this runtime check to read.
    """
    response = await client.get(FFMPEG_CHECKSUMS_URL)
    response.raise_for_status()

    best_series: str | None = None
    best_parts: tuple[int, ...] = ()
    for line in response.text.splitlines():
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


async def _fetch_component(client: httpx.AsyncClient, key: str, component: _Component) -> tuple[str, str | None]:
    """Fetch the newest upstream version of one component; None on any failure."""
    try:
        if component.source == "ffmpeg-builds":
            tag = await _fetch_latest_ffmpeg_build(client)
        else:
            tag = await _fetch_latest_release(client, component.repo)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Update check for %s failed: %s", component.repo, exc)
        return key, None
    return key, tag


async def _fetch_all(
    previous: dict[str, dict[str, Any]], attempted_at: float
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Query every upstream source once. Returns (versions, all_succeeded).

    A component that fails keeps its previously known version - with its own
    older ``checked_at``, so the result stays honest about what was actually
    confirmed just now. Entries for components that no longer exist are
    dropped instead of lingering in the cache file forever.
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
    for key, tag in results:
        if tag:
            # Stamped with the attempt's own timestamp, so a component that
            # answered is not accidentally older than the attempt itself.
            versions[key] = {"version": tag, "checked_at": attempted_at}
        else:
            complete = False
    return versions, complete


def _cache_path() -> Path:
    return get_data_dir() / CACHE_FILENAME


def _parse_timestamp(value: Any) -> float:
    """Coerce a cached timestamp to a sane float; 0.0 for anything unusable.

    Guards against a hand-edited or truncated cache file: a non-numeric value
    would otherwise raise out of the settings request, and "Infinity" would
    parse fine and then never expire.
    """
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(timestamp) or timestamp < 0.0:
        return 0.0
    return timestamp


def _read_cache_file() -> dict[str, Any] | None:
    """Load the on-disk cache; None when missing, stale-schema, or unusable."""
    try:
        raw = _cache_path().read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Discarding malformed update cache at %s", _cache_path())
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

    # A hand-edited or corrupted expires_at far in the future would otherwise
    # disable update checks indefinitely; clamp it to what a legitimate write
    # could ever have produced (see _get_latest_versions below).
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
    """Persist the cache so restarts and reloads keep the 24h window.

    Written to a temporary file in the same directory and moved into place, so
    a crash mid-write cannot leave a truncated cache behind. Single-process by
    design (the image runs WORKERS=1, see docker/Dockerfile), so no
    cross-process lock is involved.
    """
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle)
            os.replace(temp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
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
            # Only a fully successful round counts as "checked": a partial or
            # total failure keeps serving known versions, but must not claim
            # they were confirmed just now.
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
    """Link to the concrete release page for *tag*.

    ffmpeg is the exception: its builds come from a rolling release tag, so
    there is no per-version page and the link always points at that release.
    """
    if component.source == "ffmpeg-builds":
        return f"https://github.com/{component.repo}/releases/tag/latest"
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
