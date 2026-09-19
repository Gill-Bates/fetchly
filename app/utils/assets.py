#!/usr/bin/env python3
#
# app/utils/assets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cache-busting for the static assets the templates reference.

The templates used to carry hand-written tokens (``style.css?v=20260903d``)
that an author had to remember to bump in the same commit that changed the
file. That failed repeatedly and silently: a stale token means the browser
keeps serving the cached copy, so the HTML says one thing and the CSS another.
The stat tiles showed it on iOS - new markup, cached stylesheet, and the phone
caption rendered with the number's own font because the rule that sizes it was
not in the file Safari had.

So the token is derived from the file instead of typed: ``asset_url()`` hashes
the bytes, and the URL changes exactly when the content does. The hash is
cached per file and re-taken when mtime or size moves, which keeps a dev-server
edit visible without a restart and costs one ``stat()`` per reference per
render in production.

Hashed URLs are immutable by construction, so :class:`VersionedStaticFiles`
lets them be cached forever - but only while the hash still matches the file on
disk. Everything else, including the ``import "./config.js"`` specifiers that
live inside the modules and that no server-side helper can rewrite, is served
``no-cache``: the browser keeps its copy and revalidates, which costs a 304 and
can never go stale.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import Scope

__all__ = [
    "IMMUTABLE_CACHE_CONTROL",
    "REVALIDATE_CACHE_CONTROL",
    "STATIC_URL_PREFIX",
    "VERSION_QUERY_PARAM",
    "VersionedStaticFiles",
    "asset_url",
    "asset_version",
]

logger = logging.getLogger(__name__)

STATIC_URL_PREFIX = "/static/"
VERSION_QUERY_PARAM = "v"

# A year, the maximum RFC 9111 recommends, plus `immutable` so a reload does
# not revalidate either. Safe only because the URL carries the content hash.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Not "do not store": the copy is kept, it is just revalidated every time.
REVALIDATE_CACHE_CONTROL = "no-cache"

_STATIC_ROOT = Path(__file__).absolute().parent.parent / "static"
_DIGEST_LENGTH = 8

# Resolved file path -> (mtime_ns, size, digest). Unbounded by design: the key
# space is the static directory, which is fixed at build time.
_digest_cache: dict[str, tuple[int, int, str]] = {}


def _digest(file_path: Path, stat_result: os.stat_result) -> str:
    """Short content hash of *file_path*, recomputed when the file changes."""
    key = str(file_path)
    cached = _digest_cache.get(key)
    if cached is not None and cached[0] == stat_result.st_mtime_ns and cached[1] == stat_result.st_size:
        return cached[2]

    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()[:_DIGEST_LENGTH]
    _digest_cache[key] = (stat_result.st_mtime_ns, stat_result.st_size, digest)
    return digest


def _resolve(url_path: str) -> Path | None:
    """Map a ``/static/...`` URL onto its file, or None if it is not one.

    Rejects anything that escapes the static directory even though every
    caller passes a literal, because a template global is reachable from any
    template and a traversal here would leak file contents through the hash.
    """
    if not url_path.startswith(STATIC_URL_PREFIX):
        logger.warning("asset_url: %s is not under %s", url_path, STATIC_URL_PREFIX)
        return None

    relative = url_path[len(STATIC_URL_PREFIX) :].split("?", 1)[0]
    candidate = (_STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(_STATIC_ROOT.resolve())
    except ValueError:
        logger.warning("asset_url: %s escapes the static directory", url_path)
        return None
    return candidate


def asset_version(url_path: str) -> str | None:
    """Content hash for the asset behind *url_path*, or None if unreadable."""
    file_path = _resolve(url_path)
    if file_path is None:
        return None

    try:
        return _digest(file_path, file_path.stat())
    except OSError:
        # A missing asset is already a 404 for the browser; an unversioned URL
        # is the honest answer here and keeps the page rendering.
        logger.warning("asset_url: %s could not be read", url_path)
        return None


def asset_url(url_path: str) -> str:
    """``/static/style.css`` -> ``/static/style.css?v=<content hash>``."""
    version = asset_version(url_path)
    return f"{url_path}?{VERSION_QUERY_PARAM}={version}" if version else url_path


def _requested_version(scope: Scope) -> str | None:
    """The ``v`` query parameter of the request being served, if any."""
    query_string = scope.get("query_string") or b""
    values = parse_qs(query_string.decode("latin-1")).get(VERSION_QUERY_PARAM)
    return values[0] if values else None


class VersionedStaticFiles(StaticFiles):
    """Serves ``/static`` with caching bounded by the content hash.

    A request whose ``?v=`` still matches the file gets the immutable policy;
    a stale or absent token gets revalidation. That way an old page held in a
    browser cache cannot pin an old asset: its token no longer matches, so the
    next fetch of that URL revalidates and picks the change up.
    """

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)

        requested = _requested_version(scope)
        cache_control = REVALIDATE_CACHE_CONTROL
        if requested:
            try:
                current = _digest(Path(full_path), stat_result)
            except OSError:
                current = None
            if requested == current:
                cache_control = IMMUTABLE_CACHE_CONTROL

        response.headers["Cache-Control"] = cache_control
        return response
