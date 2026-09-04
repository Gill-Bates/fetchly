#!/usr/bin/env python3
#
# app/utils/changelog.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Render CHANGELOG.md to sanitized HTML for the Settings → System tile.

Markdown -> ``nh3.clean()`` with tag/attribute and URL-scheme allowlists. The
file is repo-controlled, but sanitising still stops a stray raw-HTML block
from reaching the browser.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import markdown as _markdown
import nh3

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).absolute().parent.parent.parent
_CHANGELOG_PATH = _PROJECT_ROOT / "CHANGELOG.md"

# Structural tags only - no scripts, styles, forms, media or iframes.
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr",
    "ul", "ol", "li", "a", "strong", "em", "b", "i",
    "code", "pre", "blockquote", "table", "thead", "tbody",
    "tr", "th", "td", "dl", "dt", "dd", "abbr", "sup", "sub",
    "details", "summary",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "details": {"open"},
}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

_NOT_FOUND_HTML = "<p>Changelog not found.</p>"
_ERROR_HTML = "<p>Changelog could not be rendered.</p>"


@lru_cache(maxsize=1)
def render_changelog_html() -> str:
    """Sanitized HTML for CHANGELOG.md, or a short placeholder. Cached for the
    process lifetime (the file only changes with a new build).
    """
    try:
        raw = _CHANGELOG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _NOT_FOUND_HTML
    except OSError as exc:
        logger.warning("Unable to read %s: %s", _CHANGELOG_PATH, exc)
        return _NOT_FOUND_HTML

    rendered = _markdown.markdown(raw, extensions=["extra", "sane_lists"])
    return nh3.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_ALLOWED_URL_SCHEMES,
        strip_comments=True,
    )


def get_changelog_html() -> str:
    """Best-effort changelog HTML for templates - never raises."""
    try:
        return render_changelog_html()
    except Exception:
        logger.warning("Failed to render changelog", exc_info=True)
        return _ERROR_HTML
