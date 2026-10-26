#!/usr/bin/env python3
#
# app/utils/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Version and build information for fetchly."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).absolute().parent.parent.parent
_DIST_NAME = "fetchly"  # pyproject distribution name, for the installed fallback


def _read_file(name: str) -> str | None:
    """Read *name* from the project root. Return None if missing or unreadable."""
    try:
        return (_PROJECT_ROOT / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Unable to read %s from %s: %s", name, _PROJECT_ROOT, e)
        return None


def _version_from_pyproject() -> str | None:
    """Return ``[project] version`` from pyproject.toml, or None.

    pyproject.toml is the single source of truth: the release workflow checks
    the git tag against it, and the image bundles it.
    """
    path = _PROJECT_ROOT / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        logger.debug("Unable to read %s: %s", path, exc)
        return None
    except tomllib.TOMLDecodeError as exc:
        logger.warning("pyproject.toml is not valid TOML (%s); version falls back", exc)
        return None

    version = data.get("project", {}).get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()

    logger.warning("pyproject.toml has no [project] version entry")
    return None


@lru_cache(maxsize=1)
def get_version() -> str:
    """Application version from pyproject.toml, then installed distribution
    metadata, then 'dev'.
    """
    version = _version_from_pyproject()
    if version:
        return version

    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        logger.debug("%s is not installed as a distribution; version is 'dev'", _DIST_NAME)
        return "dev"


@lru_cache(maxsize=1)
def get_build_info() -> str:
    """Return build info from the BUILD_INFO file, or 'dev'."""
    return _read_file("BUILD_INFO") or "dev"


_FFMPEG_VERSION_RE = re.compile(r"ffmpeg version (\S+)")
_DENO_VERSION_RE = re.compile(r"deno (\S+)")


def _tool_version(cmd: list[str], *, label: str, timeout: float, pattern: re.Pattern[str] | None) -> str:
    """Run a ``--version`` style command; return the version or 'unavailable'.

    Without ``pattern`` the stripped stdout is the version; with it, the first
    capture group searched in the first stdout line.
    """
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Unable to read %s version: %s", label, exc)
        return "unavailable"

    stdout = result.stdout or ""
    if pattern is None:
        return stdout.strip() or "unavailable"
    first_line = stdout.splitlines()[0] if stdout else ""
    match = pattern.search(first_line)
    return match.group(1) if match else "unavailable"


# Tool versions. Each shells out and blocks (up to its timeout) on the first
# call; lru_cache makes later calls free. Async callers use asyncio.to_thread().
@lru_cache(maxsize=1)
def get_ytdlp_version() -> str:
    """Installed yt-dlp version, or 'unavailable'."""
    return _tool_version(["yt-dlp", "--version"], label="yt-dlp", timeout=3, pattern=None)


@lru_cache(maxsize=1)
def get_ffmpeg_version() -> str:
    """Installed ffmpeg version, or 'unavailable'."""
    return _tool_version(["ffmpeg", "-version"], label="ffmpeg", timeout=3, pattern=_FFMPEG_VERSION_RE)


@lru_cache(maxsize=1)
def get_ytdlp_ejs_version() -> str:
    """Installed yt-dlp-ejs version, or 'unavailable' (needed for full YouTube
    support, alongside a JS runtime).
    """
    try:
        return metadata.version("yt-dlp-ejs")
    except metadata.PackageNotFoundError:
        logger.warning("yt-dlp-ejs is not installed; YouTube support may be degraded")
        return "unavailable"


@lru_cache(maxsize=1)
def get_js_runtime_version() -> str:
    """deno version yt-dlp-ejs runs on, or 'unavailable' (deno is the only JS
    runtime yt-dlp enables by default; just needs to be on PATH).
    """
    return _tool_version(["deno", "--version"], label="deno", timeout=5, pattern=_DENO_VERSION_RE)


# The pin is a committed text file: docker/wavesurfer.version in the checkout,
# copied to /app/.wavesurfer_version in the image.
_WAVESURFER_VERSION_FILES = (".wavesurfer_version", "docker/wavesurfer.version")
_WAVESURFER_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")


@lru_cache(maxsize=1)
def get_wavesurfer_version() -> str:
    """WaveSurfer.js version: ENV WAVESURFER_VERSION (exported by the container
    entrypoint), else the committed pin file, else 'unavailable'.

    The file fallback covers every start that bypasses the entrypoint (local
    dev server, ``docker exec``, a custom command); the vendored bundle has no
    marker of its own.
    """
    env_value = (os.environ.get("WAVESURFER_VERSION") or "").strip()
    if env_value:
        return env_value
    for name in _WAVESURFER_VERSION_FILES:
        value = _read_file(name)
        if value and _WAVESURFER_VERSION_RE.fullmatch(value):
            return value
    return "unavailable"


# VERSION and BUILD_INFO are lazy: I/O is deferred to first attribute access.
def __getattr__(name: str) -> str:
    if name == "VERSION":
        return get_version()
    if name == "BUILD_INFO":
        return get_build_info()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), "VERSION", "BUILD_INFO"})


if TYPE_CHECKING:  # so version.VERSION is typed as str
    VERSION: str
    BUILD_INFO: str
