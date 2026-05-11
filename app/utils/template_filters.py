#!/usr/bin/env python3
#
# app/utils/template_filters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Jinja2 template filters and template-safe helpers for TubeYou."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "LOCAL_TZ",
    "FileSize",
    "filesize",
    "is_lalala_configured",
    "localtime",
    "mask_secret",
    "public_settings",
    "register_filters",
    "status_class",
    "status_icon",
]


def _resolve_local_tz() -> ZoneInfo:
    """Resolve the local timezone from the ``TZ`` environment variable."""
    tz_name = os.environ.get("TZ", "UTC")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


LOCAL_TZ = _resolve_local_tz()

_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "analysis_done"})
_STATUS_CLASS_MAP: dict[str, str] = {
    "cancelled": "secondary",
    "error": "danger",
}
_STATUS_ICON_MAP: dict[str, str] = {
    "cancelled": "cancel",
    "error": "error",
    "analysis": "graphic_eq",
}
_FILESIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1_099_511_627_776),
    ("GiB", 1_073_741_824),
    ("MiB", 1_048_576),
    ("KiB", 1_024),
)

_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_key",
    "_secret",
    "_password",
    "_hash",
    "_token",
)
_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = ("secret", "password", "token")

_INTERNAL_PUBLIC_EXCLUDE: frozenset[str] = frozenset({
    "admin_password_hash",
    "session_version",
})


@dataclass(frozen=True, slots=True)
class FileSize:
    value: str
    unit: str
    display: str

    def __str__(self) -> str:
        return self.display


def _format_filesize(value: int | None) -> FileSize:
    if value is None:
        return FileSize(value="", unit="", display="–")
    if value == 0:
        return FileSize(value="0", unit="B", display="0 B")
    for unit, divisor in _FILESIZE_UNITS:
        if value >= divisor:
            precision = 2 if unit in {"TiB", "GiB"} else 1
            formatted = f"{value / divisor:.{precision}f}"
            return FileSize(value=formatted, unit=unit, display=f"{formatted} {unit}")
    return FileSize(value=str(value), unit="B", display=f"{value} B")


def localtime(value: str | None) -> Markup:
    """Convert an ISO datetime string to localized HTML."""
    if not value:
        return Markup("")
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local_dt = dt.astimezone(LOCAL_TZ)
        date_str = escape(f"{local_dt:%d.%m.%Y}")
        time_str = escape(f"{local_dt:%H:%M}")
        return Markup(
            f'<span class="date-part">{date_str}</span> '
            f'<span class="time-part">{time_str}</span>'
        )
    except (ValueError, TypeError):
        return escape(str(value))


def filesize(value: int | None) -> FileSize:
    """Convert bytes to a structured human-readable filesize."""
    return _format_filesize(value)


def status_class(status: str | None) -> str:
    """Get Bootstrap class for job status."""
    if status in _TERMINAL_STATUSES:
        return "success"
    return _STATUS_CLASS_MAP.get(status, "primary")


def status_icon(status: str | None) -> str:
    """Get Material icon name for job status."""
    if status in _TERMINAL_STATUSES:
        return "check_circle"
    return _STATUS_ICON_MAP.get(status, "schedule")


def mask_secret(value: str | None) -> str:
    """Mask a sensitive credential value.

    Values longer than 8 characters keep the first and last 4 characters.
    """
    if not value:
        return ""
    secret = str(value).strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered.endswith(_SENSITIVE_KEY_SUFFIXES)
        or any(token in lowered for token in _SENSITIVE_KEY_SUBSTRINGS)
    )


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Create a public-safe shallow copy of settings with sensitive fields masked."""
    public = dict(settings)
    for key in _INTERNAL_PUBLIC_EXCLUDE:
        public.pop(key, None)
    for key, value in list(public.items()):
        if _is_sensitive_key(key):
            public[key] = mask_secret(value)
    # Use the original settings so the configured flag is based on the real
    # auth key, not the masked copy in `public`.
    public["lalalaai_configured"] = is_lalala_configured(settings)
    return public


def is_lalala_configured(settings: dict[str, Any]) -> bool:
    """Check if Lalal.ai credentials are present."""
    return bool(
        str(settings.get("lalalaai_email", "")).strip()
        and str(settings.get("lalalaai_auth_key", "")).strip()
    )


def register_filters(templates: Jinja2Templates) -> None:
    """Register all template filters with a Jinja2Templates instance."""
    templates.env.filters["localtime"] = localtime
    templates.env.filters["filesize"] = filesize
    templates.env.filters["status_class"] = status_class
    templates.env.filters["status_icon"] = status_icon
