#!/usr/bin/env python3
#
# app/utils/updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Upstream release checks for the tools shown on the settings page.

Each component is compared against the source the image actually installs it
from — GitHub releases for the packages, the newest BtbN autobuild of the installed
stable series for ffmpeg. The answer is cached for 24 hours (in memory plus a small JSON file in
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
CACHE_SCHEMA = 5
CACHE_TTL_SECONDS = 24 * 60 * 60  # after a successful check
# Negative cache: only the components that failed are asked again, so a short
# TTL costs a handful of requests (GitHub allows 60/h per IP unauthenticated).
RETRY_TTL_SECONDS = 15 * 60
# Hard ceiling on the whole concurrent fetch-all round. httpx's per-phase
# timeout does not bound a redirect chain, so this is the real deadline every
# /api/updates request can wait on via _lock.
_TOTAL_FETCH_TIMEOUT = 20.0
# Per-component deadline, so one slow upstream cannot cancel the others' answers.
_COMPONENT_TIMEOUT = 15.0

_REQUEST_TIMEOUT = 6.0
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "fetchly-update-check",
}

# BtbN publishes a rolling "latest" release (assets named "...-n9.0-latest-...",
# no build id) and immutable dated "autobuild-*" releases whose assets carry the
# real build, e.g. "ffmpeg-n9.0.2-36-g6882969ce9-linux64-gpl-9.0.tar.xz". The
# release workflow pins the newest autobuild, so that is what we compare against.
FFMPEG_BUILDS_REPO = "BtbN/FFmpeg-Builds"
FFMPEG_TAGS_URL = f"https://api.github.com/repos/{FFMPEG_BUILDS_REPO}/tags?per_page=10"
FFMPEG_DOWNLOAD_BASE = f"https://github.com/{FFMPEG_BUILDS_REPO}/releases/download"
# Stable GPL asset: group 1 the build ("n9.0.2-36-g6882969ce9"), group 2 the
# arch, group 3 the series. The trailing series keeps out the "N-<rev>" master
# builds, which carry none.
_FFMPEG_ASSET_RE = re.compile(
    r"^ffmpeg-(n\d+(?:\.\d+)*(?:-\d+-g[0-9a-f]+)?)-(linux64|linuxarm64)-gpl-(\d+(?:\.\d+)*)\.tar\.xz$"
)
# Installed/upstream ffmpeg id: "n9.0.2-12-gc867e13549-20260927" -> release
# "9.0.2", commits since the tag "12". The trailing date is ignored.
_FFMPEG_BUILD_RE = re.compile(r"^[nN]?(\d+(?:\.\d+)*)(?:-(\d+)-g[0-9a-f]+)?")
_MAX_FFMPEG_AUTOBUILDS = 3
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
    "ffmpeg": _Component(
        label="ffmpeg", repo=FFMPEG_BUILDS_REPO, source="ffmpeg-builds"
    ),
    "wavesurfer": _Component(
        label="wavesurfer.js", repo="katspaugh/wavesurfer.js", source="releases"
    ),
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
    return bool(
        re.search(
            r"[-_.](?:alpha|beta|rc|dev|pre|a|b)\d*(?:[-+_.]|$)", tag, re.IGNORECASE
        )
    )


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
# budget: 2 x 20 releases / 3 x 100 tags is more than any configured repo needs.
_MAX_RELEASE_PAGES = 2
_MAX_TAG_PAGES = 3
# Releases carry their full changelog text: 20 of yt-dlp's are ~1 MB and 20 of
# deno's ~1.6 MB, so 50 per page (as used before) exceeded the old 1 MiB cap and
# made those two checks fail every time. Bound memory, but with headroom.
_RELEASES_PER_PAGE = 20
_MAX_API_RESPONSE_BYTES = 4 * 1024 * 1024
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
    url: str | None = (
        f"https://api.github.com/repos/{repo}/releases?per_page={_RELEASES_PER_PAGE}"
    )

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

    for _ in range(_MAX_TAG_PAGES):
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


def _ffmpeg_build_key(value: str | None) -> tuple[tuple[int, ...], int] | None:
    """Comparable ``(release numbers, commits since tag)`` of an ffmpeg build id."""
    match = _FFMPEG_BUILD_RE.match((value or "").strip())
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    return release, int(match.group(2) or 0)


def _ffmpeg_series(value: str | None) -> str | None:
    """Series ("9.0") of an ffmpeg build id such as "n9.0.2-12-gabc"."""
    key = _ffmpeg_build_key(value)
    if not key or len(key[0]) < 2:
        return None
    return ".".join(str(part) for part in key[0][:2])


def _parse_ffmpeg_builds(checksums: str) -> dict[str, str]:
    """Map series -> build id for series published for both architectures."""
    found: dict[str, dict[str, str]] = {}
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        match = _FFMPEG_ASSET_RE.match(parts[1])
        if match:
            found.setdefault(match.group(3), {})[match.group(2)] = match.group(1)
    return {
        series: archs["linux64"]
        for series, archs in found.items()
        if len(archs) == 2 and archs["linux64"] == archs["linuxarm64"]
    }


async def _fetch_latest_ffmpeg_build(
    client: httpx.AsyncClient,
) -> tuple[str | None, dict[str, str]]:
    """Return ``(newest build, {series: build})`` of the newest usable autobuild.

    Mirrors the release workflow: newest dated autobuild whose checksums list a
    stable series for both architectures. The Dockerfile's series is not
    recorded in the image, so the caller matches the installed series.
    """
    _, body = await _read_response_bytes(
        client, FFMPEG_TAGS_URL, max_bytes=_MAX_API_RESPONSE_BYTES
    )
    payload = json.loads(body)
    if not isinstance(payload, list):
        return None, {}
    tags = sorted(
        {
            name
            for entry in payload
            if isinstance(entry, dict)
            and (name := str(entry.get("name") or "")).startswith("autobuild-")
        },
        reverse=True,
    )

    for tag in tags[:_MAX_FFMPEG_AUTOBUILDS]:
        _, checksums = await _read_response_bytes(
            client,
            f"{FFMPEG_DOWNLOAD_BASE}/{quote(tag, safe='')}/checksums.sha256",
            max_bytes=_MAX_FFMPEG_CHECKSUM_BYTES,
        )
        builds = _parse_ffmpeg_builds(checksums.decode("utf-8"))
        if builds:
            newest = max(builds, key=lambda series: _parse_version(series) or ())
            return builds[newest], builds
    return None, {}


async def _fetch_component(
    client: httpx.AsyncClient, key: str, component: _Component
) -> tuple[str, str | None, bool, dict[str, str]]:
    """Fetch the newest upstream version of one component.

    Returns ``(key, tag, retry, series)`` (*series* only for ffmpeg). ``retry`` is True only for transient
    failures (blip, timeout, rate limit) - a 404 is False, so a still-private
    repo does not drag the whole batch onto the short RETRY_TTL cadence.
    """
    series: dict[str, str] = {}
    try:
        async with asyncio.timeout(_COMPONENT_TIMEOUT):
            if component.source == "ffmpeg-builds":
                tag, series = await _fetch_latest_ffmpeg_build(client)
            elif component.source == "tags":
                tag = await _fetch_latest_tag(client, component.repo)
            else:
                tag = await _fetch_latest_release(client, component.repo)
    except TimeoutError:
        logger.warning(
            "Update check for %s exceeded its %.0fs deadline",
            component.repo,
            _COMPONENT_TIMEOUT,
        )
        return key, None, True, {}
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
        return key, None, retry, {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Update check for %s failed: %s", component.repo, exc)
        return key, None, True, {}
    return key, tag, False, series


async def _fetch_all(
    previous: dict[str, dict[str, Any]],
    attempted_at: float,
    keys: frozenset[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Query the upstream sources of *keys* (default: all) once.

    Returns ``(versions, all_succeeded)``. Components outside *keys* keep their
    entry untouched and do not count towards ``all_succeeded``.

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
            *(
                _fetch_component(client, key, component)
                for key, component in COMPONENTS.items()
                if keys is None or key in keys
            ),
            return_exceptions=True,
        )

    versions = {key: entry for key, entry in previous.items() if key in COMPONENTS}
    complete = True
    for result in results:
        if isinstance(result, BaseException):
            # A bug in one lookup must not take the other answers with it.
            logger.error("Update check crashed: %r", result, exc_info=result)
            complete = False
            continue
        key, tag, retry, series = result
        if tag:
            versions[key] = {"version": tag, "checked_at": attempted_at}
            if series:
                versions[key]["series"] = series
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
            raw_series = entry.get("series")
            if isinstance(raw_series, dict):
                versions[str(key)]["series"] = {
                    str(k): str(v) for k, v in raw_series.items() if isinstance(v, str)
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
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
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


def _keys_to_refresh(cache: dict[str, Any] | None, now: float) -> frozenset[str] | None:
    """Components worth asking again; None means all of them.

    After an incomplete round only the failed ones are retried (those without
    an answer, or whose answer predates the failed attempt) - the successful
    ones keep their 24h window instead of burning the unauthenticated GitHub
    rate limit on every retry.
    """
    if not cache or cache.get("complete"):
        return None
    versions: dict[str, dict[str, Any]] = cache.get("versions", {})
    last_attempt_at = float(cache.get("last_attempt_at", 0.0))
    stale_before = now - CACHE_TTL_SECONDS
    keys = frozenset(
        key
        for key in COMPONENTS
        if key not in versions
        or float(versions[key].get("checked_at", 0.0)) < last_attempt_at
        or float(versions[key].get("checked_at", 0.0)) < stale_before
    )
    return keys or None


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

        previous: dict[str, dict[str, Any]] = (
            dict(_cache.get("versions", {})) if _cache else {}
        )
        previous_checked_at = float(_cache.get("checked_at", 0.0)) if _cache else 0.0
        keys = None if force else _keys_to_refresh(_cache, now)

        attempted_at = time.time()
        try:
            async with asyncio.timeout(_TOTAL_FETCH_TIMEOUT):
                versions, complete = await _fetch_all(previous, attempted_at, keys)
        except TimeoutError:
            logger.warning(
                "Upstream update check exceeded its %.0fs deadline",
                _TOTAL_FETCH_TIMEOUT,
            )
            versions, complete = previous, False

        entry = {
            "schema": CACHE_SCHEMA,
            # Only a fully successful round advances "checked_at".
            "checked_at": attempted_at if complete else previous_checked_at,
            "last_attempt_at": attempted_at,
            "complete": complete,
            "expires_at": time.time()
            + (CACHE_TTL_SECONDS if complete else RETRY_TTL_SECONDS),
            "versions": versions,
        }
        _cache = entry
        await asyncio.to_thread(_write_cache_file, entry)
        return entry


def _ffmpeg_is_newer(latest: str | None, current: str | None) -> bool:
    """True when the *latest* ffmpeg build is strictly newer than *current*."""
    latest_key = _ffmpeg_build_key(latest)
    current_key = _ffmpeg_build_key(current)
    if not latest_key or not current_key:
        return False
    length = max(len(latest_key[0]), len(current_key[0]))
    pad = lambda numbers: numbers + (0,) * (length - len(numbers))  # noqa: E731
    return (pad(latest_key[0]), latest_key[1]) > (pad(current_key[0]), current_key[1])


def _status(
    update_available: bool, comparable: bool
) -> Literal["available", "current", "unknown"]:
    if update_available:
        return "available"
    return "current" if comparable else "unknown"


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
        if component.source == "ffmpeg-builds":
            # No build for the installed series means unknown, never a cross-series update.
            series_builds = cached.get("series") or {}
            latest = series_builds.get(_ffmpeg_series(installed) or "")
            update_available = _ffmpeg_is_newer(latest, installed)
            installed_ok = _ffmpeg_build_key(installed) is not None
            comparable = _ffmpeg_build_key(latest) is not None and installed_ok
        else:
            update_available = _is_newer(latest, installed)
            installed_ok = _parse_version(installed) is not None
            comparable = _parse_version(latest) is not None and installed_ok
        if update_available or comparable:
            reason = ""
        elif not installed_ok:
            reason = "installed_unknown"
        else:
            reason = "latest_unknown"
        components[key] = {
            "label": component.label,
            "current": installed,
            "latest": latest or "",
            "update_available": update_available,
            # "unknown" = no answer or nothing comparable; never a guess.
            "status": _status(update_available, comparable),
            "url": _release_url(component, latest),
            "checked_at": checked_at,
            "stale": bool(latest) and checked_at < last_attempt_at,
            # Why status is "unknown": "installed_unknown" or "latest_unknown".
            "reason": reason,
        }

    return {
        "checked_at": float(entry.get("checked_at", 0.0)),
        "last_attempt_at": last_attempt_at,
        "complete": bool(entry.get("complete")),
        "components": components,
    }
