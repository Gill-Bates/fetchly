#!/usr/bin/env python3
#
# app/utils/boolean.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The accepted spellings of a textual boolean (settings, API payloads, environment)."""

from __future__ import annotations

from typing import Final

__all__ = ["parse_bool_text"]

_TRUE_TEXT: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE_TEXT: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def parse_bool_text(value: str) -> bool | None:
    """``True``/``False`` for a recognised spelling, ``None`` for anything else.

    Callers keep their own error type and message for the unrecognised case.
    """
    normalized = value.strip().lower()
    if normalized in _TRUE_TEXT:
        return True
    if normalized in _FALSE_TEXT:
        return False
    return None
