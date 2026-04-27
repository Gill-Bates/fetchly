#!/usr/bin/env python3
#
# app/utils/toast.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server-side toast message utilities.

This module provides helpers to pass toast notifications from Python
to the frontend via template context or response headers.

Usage in routes:
    from app.utils.toast import add_toast, get_toasts

    @app.get("/example")
    def example(request: Request):
        add_toast(request, "Settings saved", "success")
        return templates.TemplateResponse(...)

In templates:
    {% for t in toasts %}
    <script>showToast("{{ t.message }}", "{{ t.type }}");</script>
    {% endfor %}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from fastapi import Request

ToastType = Literal["success", "danger", "warning", "info"]

_TOAST_STATE_KEY: Final = "toast_messages"


def _get_toast_list(request: "Request") -> list[ToastMessage]:
    """Return the mutable toast list from request state, creating it if absent."""
    toasts = getattr(request.state, _TOAST_STATE_KEY, None)
    if toasts is None:
        toasts = []
        setattr(request.state, _TOAST_STATE_KEY, toasts)
    return toasts


@dataclass(frozen=True, slots=True)
class ToastMessage:
    """A toast notification message."""

    message: str
    type: ToastType = "info"
    duration: int = 3000

    def to_dict(self) -> dict[str, str | int]:
        """Convert to dictionary for JSON serialization."""
        return {"message": self.message, "type": self.type, "duration": self.duration}


def add_toast(
    request: "Request",
    message: str,
    toast_type: ToastType = "info",
    duration: int = 3000,
) -> None:
    """Add a toast message to the request state.

    Args:
        request: The FastAPI request object
        message: The toast message text
        toast_type: One of "success", "danger", "warning", "info"
        duration: Display duration in milliseconds (0 = no auto-dismiss)
    """
    _get_toast_list(request).append(
        ToastMessage(message=message, type=toast_type, duration=duration)
    )


def get_toasts(request: "Request") -> list[ToastMessage]:
    """Get all toast messages from request state.

    Args:
        request: The FastAPI request object

    Returns:
        List of ToastMessage objects
    """
    return list(getattr(request.state, _TOAST_STATE_KEY, []))


def get_toasts_json(request: "Request") -> list[dict[str, str | int]]:
    """Get all toast messages as JSON-serializable dicts.

    Args:
        request: The FastAPI request object

    Returns:
        List of toast dictionaries
    """
    return [t.to_dict() for t in get_toasts(request)]


def clear_toasts(request: "Request") -> None:
    """Clear all toast messages from request state."""
    setattr(request.state, _TOAST_STATE_KEY, [])


# Convenience functions
def toast_success(request: "Request", message: str, duration: int = 3000) -> None:
    """Add a success toast message."""
    add_toast(request, message, "success", duration)


def toast_error(request: "Request", message: str, duration: int = 5000) -> None:
    """Add an error toast message (longer duration by default)."""
    add_toast(request, message, "danger", duration)


def toast_warning(request: "Request", message: str, duration: int = 4000) -> None:
    """Add a warning toast message."""
    add_toast(request, message, "warning", duration)


def toast_info(request: "Request", message: str, duration: int = 3000) -> None:
    """Add an info toast message."""
    add_toast(request, message, "info", duration)
