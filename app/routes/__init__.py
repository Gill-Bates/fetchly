#!/usr/bin/env python3
#
# app/routes/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Route modules for the TubeYou application."""

from __future__ import annotations

from .auth import router as auth_router
from .api import router as api_router
from .events import router as events_router
from .lalal import router as lalal_router
from .media import router as media_router
from .trim import router as trim_router

__all__ = [
    "auth_router",
    "api_router",
    "events_router",
    "lalal_router",
    "media_router",
    "trim_router",
]
