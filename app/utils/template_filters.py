#!/usr/bin/env python3
#
# app/utils/template_filters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Jinja2 template filters and template-safe helpers for Fetchly."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ..db import COMPLETED_STATUSES
from .duration import format_clock
from .platform import detect_platform, platform_label

__all__ = [
    "LOCAL_TZ",
    "FileSize",
    "filesize",
    "is_lalala_configured",
    "localtime",
    "platform_id",
    "platform_pill",
    "public_settings",
    "register_filters",
    "status_class",
    "status_icon",
    "status_label",
]


logger = logging.getLogger(__name__)


def _resolve_local_tz() -> ZoneInfo:
    """Resolve the local timezone from the ``TZ`` environment variable.

    An unset ``TZ`` means UTC. A configured but unusable ``TZ`` still falls back
    to UTC, but is logged: silently showing UTC timestamps as local time makes
    every displayed time wrong without any visible symptom.
    """
    tz_name = os.environ.get("TZ", "").strip()
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        logger.warning("Invalid TZ setting %r (%s); falling back to UTC", tz_name, exc)
        return ZoneInfo("UTC")


LOCAL_TZ = _resolve_local_tz()

_SUCCESS_STATUSES = COMPLETED_STATUSES
_STATUS_CLASS_MAP: dict[str, str] = {
    "cancelled": "secondary",
    "error": "danger",
}
# Human-readable phase names for the status pill. Raw status values leak
# implementation wording ("processing") and the previous client-side collapse of
# every in-flight status into "Running" hid which phase a job was actually in.
# Keep in sync with STATUS_META in app/static/js/ui.js.
_STATUS_LABEL_MAP: dict[str, str] = {
    "queued": "Queued",
    "processing": "Preparing",
    "downloading": "Downloading",
    "transcoding": "Transcoding",
    "analysis": "Analyzing",
    "analysis_done": "Done",
    "done": "Done",
    "error": "Error",
    "cancelled": "Cancelled",
}
_STATUS_ICON_MAP: dict[str, str] = {
    "cancelled": "cancel",
    "error": "error",
    "analysis": "graphic_eq",
    "queued": "schedule",
    "processing": "sync",
    "downloading": "download",
    "transcoding": "memory",
}
_FILESIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1_099_511_627_776),
    ("GiB", 1_073_741_824),
    ("MiB", 1_048_576),
    ("KiB", 1_024),
)

# Explicit allowlist rather than a denylist copy-then-mask: a setting added to
# _SETTINGS_DEFAULTS (app/db.py) without a matching entry here simply never
# reaches a template or the JSON /api/settings response, instead of relying on
# every future secret's name containing "key"/"secret"/"password"/"token".
_PUBLIC_SETTING_KEYS: frozenset[str] = frozenset({
    "retention_days",
    "login_required",
    "enable_authentication",
    "session_idle_minutes",
    "download_concurrent_fragments",
    "download_worker_count",
    "download_timeout_minutes",
    "transcode_timeout_minutes",
    "download_max_filesize_gib",
    "download_mp4_preset",
    "video_watermark",
    "audio_analysis_max_minutes",
    "audio_analysis_timeout_minutes",
    "share_link_max_uses",
    "public_hostname",
    "lalalaai_email",
    "lalalaai_auth_checked_at",
    "lalalaai_auth_is_valid",
    "lalalaai_duration_guard",
    "lalal_max_download_gib",
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
            formatted = f"{value / divisor:.1f}"
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
        # Both halves went through escape() above, so the only markup here is
        # the span wrapper this filter exists to add.
        return Markup(  # noqa: S704
            f'<span class="date-part">{date_str}</span> '
            f'<span class="time-part">{time_str}</span>'
        )
    except (ValueError, TypeError):
        return escape(str(value))


def filesize(value: int | None) -> FileSize:
    """Convert bytes to a structured human-readable filesize."""
    return _format_filesize(value)


def duration(value: Any) -> str:
    """Render a stored duration as a clock time for a template."""
    return format_clock(value)


def status_class(status: str | None) -> str:
    """Get Bootstrap class for job status."""
    if status in _SUCCESS_STATUSES:
        return "success"
    return _STATUS_CLASS_MAP.get(status, "primary")


def status_label(status: str | None) -> str:
    """Get the human-readable phase label for a job status."""
    if not status:
        return _STATUS_LABEL_MAP["queued"]
    return _STATUS_LABEL_MAP.get(status, str(status).replace("_", " ").title())


def status_icon(status: str | None) -> str:
    """Get Material icon name for job status."""
    if status in _SUCCESS_STATUSES:
        return "check_circle"
    return _STATUS_ICON_MAP.get(status, "schedule")


def platform_pill(url: str | None) -> str:
    """Return the short platform pill label (YT / TikTok / Insta / FB) for a URL."""
    return platform_label(detect_platform(url))


def platform_id(url: str | None) -> str:
    """Return the platform id (youtube / tiktok / instagram / facebook) for a URL."""
    return detect_platform(url) or ""


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Build the public-safe view of settings for templates and /api/settings.

    Allowlisted keys only - credentials such as lalalaai_auth_key are never
    included, not even partially. Callers that need to know whether Lalal.ai
    is configured get the boolean flag instead of any part of the key.
    """
    public = {key: settings[key] for key in _PUBLIC_SETTING_KEYS if key in settings}
    public["lalalaai_configured"] = is_lalala_configured(settings)
    return public


def is_lalala_configured(settings: dict[str, Any]) -> bool:
    """Check if Lalal.ai credentials are present."""
    email = settings.get("lalalaai_email")
    auth_key = settings.get("lalalaai_auth_key")
    return (
        isinstance(email, str)
        and bool(email.strip())
        and isinstance(auth_key, str)
        and bool(auth_key.strip())
    )


def register_filters(templates: Jinja2Templates) -> None:
    """Register all template filters with a Jinja2Templates instance."""
    templates.env.filters["localtime"] = localtime
    templates.env.filters["filesize"] = filesize
    templates.env.filters["duration"] = duration
    templates.env.filters["status_class"] = status_class
    templates.env.filters["status_label"] = status_label
    templates.env.filters["status_icon"] = status_icon
    templates.env.filters["platform_pill"] = platform_pill
    templates.env.filters["platform_id"] = platform_id
