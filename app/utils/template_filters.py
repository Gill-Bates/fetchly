#!/usr/bin/env python3
#
# app/utils/template_filters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Jinja2 template filters and template-safe helpers for Fetchly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ..db import _JOB_STATUSES as ALL_JOB_STATUSES
from ..db import (
    CANCELLABLE_JOB_STATUSES,
    COMPLETED_STATUSES,
    DOWNLOADABLE_STATUSES,
    PROGRESS_STATUSES,
    RETRYABLE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    to_iso_utc,
)
from .duration import format_clock
from .platform import detect_platform, platform_label

__all__ = [
    "FileSize",
    "filesize",
    "is_lalala_configured",
    "localtime",
    "platform_id",
    "platform_pill",
    "public_settings",
    "register_filters",
    "status_class",
    "status_contract",
    "status_label",
]


_SUCCESS_STATUSES = COMPLETED_STATUSES
_STATUS_CLASS_MAP: dict[str, str] = {
    "cancelled": "secondary",
    "error": "danger",
}
# Phase names for the status pill. Keep in sync with STATUS_META in
# app/static/js/ui.js.
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
_FILESIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1_099_511_627_776),
    ("GiB", 1_073_741_824),
    ("MiB", 1_048_576),
    ("KiB", 1_024),
)

# Allowlist, not a denylist: a new setting is invisible to templates and
# /api/settings until it is added here - no reliance on secret-y key names.
_PUBLIC_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "retention_days",
        "enable_job_history",
        "download_auto_start",
        "login_required",
        "enable_authentication",
        "session_max_days",
        "download_concurrent_fragments",
        "download_worker_count",
        "download_timeout_minutes",
        "transcode_timeout_minutes",
        "download_max_filesize_gib",
        "download_output_mode",
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
    }
)


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
    """Render a stored UTC timestamp as ``<time>`` for browser-side localization.

    The server never picks a time zone: ``datetime`` carries the UTC instant
    and ``utils.js::localizeValues`` formats it in the viewer's locale and
    zone; the UTC text inside is the no-JS fallback.
    """
    iso = to_iso_utc(value)
    if iso is None:
        return escape(str(value)) if value else Markup("")
    dt = datetime.fromisoformat(iso)
    date_str = escape(f"{dt:%Y-%m-%d}")
    time_str = escape(f"{dt:%H:%M} UTC")
    # Every interpolated part went through escape(); the only markup is the
    # wrapper this filter exists to add.
    return Markup(  # noqa: S704
        f'<time datetime="{escape(iso)}" data-localize>'
        f'<span class="date-part">{date_str}</span> <span class="time-part">{time_str}</span></time>'
    )


def filesize(value: int | None) -> FileSize:
    return _format_filesize(value)


def duration(value: Any) -> str:
    return format_clock(value)


def status_class(status: str | None) -> str:
    """Bootstrap contextual class for a job status."""
    if status in _SUCCESS_STATUSES:
        return "success"
    return _STATUS_CLASS_MAP.get(status, "primary")


def status_label(status: str | None) -> str:
    """Human-readable phase label for a job status."""
    if not status:
        return _STATUS_LABEL_MAP["queued"]
    return _STATUS_LABEL_MAP.get(status, str(status).replace("_", " ").title())


def status_contract() -> dict[str, Any]:
    """The job-status contract the frontend reads from page bootstrap data.

    The authoritative groups live in app/db.py and the display metadata in
    status_label()/status_class() above; this is the single place that
    serializes both for the browser so config.js and ui.js no longer keep
    their own copies. Sets are emitted as sorted lists for a stable, diffable
    JSON payload. Guarded against drift by tests/test_status_mapping_parity.py.
    """
    return {
        "downloadable": sorted(DOWNLOADABLE_STATUSES),
        "cancellable": sorted(CANCELLABLE_JOB_STATUSES),
        "retryable": sorted(RETRYABLE_JOB_STATUSES),
        "terminal": sorted(TERMINAL_JOB_STATUSES),
        "progress": sorted(PROGRESS_STATUSES),
        "meta": {
            status: {"label": status_label(status), "color": status_class(status)}
            for status in sorted(ALL_JOB_STATUSES)
        },
    }


def platform_pill(url: str | None) -> str:
    """Return the short platform pill label (YT / TikTok / Insta / FB) for a URL."""
    return platform_label(detect_platform(url))


def platform_id(url: str | None) -> str:
    """Return the platform id (youtube / tiktok / instagram / facebook) for a URL."""
    return detect_platform(url) or ""


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """The public-safe view of settings for templates and /api/settings:
    allowlisted keys plus a ``lalalaai_configured`` boolean, never the key.
    """
    public = {key: settings[key] for key in _PUBLIC_SETTING_KEYS if key in settings}
    public["lalalaai_configured"] = is_lalala_configured(settings)
    return public


def is_lalala_configured(settings: dict[str, Any]) -> bool:
    """Whether both Lalal.ai credentials (email + auth key) are present."""
    email = settings.get("lalalaai_email")
    auth_key = settings.get("lalalaai_auth_key")
    return isinstance(email, str) and bool(email.strip()) and isinstance(auth_key, str) and bool(auth_key.strip())


def register_filters(templates: Jinja2Templates) -> None:
    templates.env.filters["localtime"] = localtime
    templates.env.filters["filesize"] = filesize
    templates.env.filters["duration"] = duration
    templates.env.filters["status_class"] = status_class
    templates.env.filters["status_label"] = status_label
    templates.env.filters["platform_pill"] = platform_pill
    templates.env.filters["platform_id"] = platform_id
    # Exposed as a global (not a filter) so every page's base.html can embed the
    # status contract in its bootstrap data without the route handlers passing it.
    templates.env.globals["status_contract"] = status_contract
