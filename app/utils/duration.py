#!/usr/bin/env python3
#
# app/utils/duration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Central rounding for the second values the app stores and compares.

Every duration in fetchly - what ffprobe reports, what the jobs table holds,
what a trim selection is validated against - passes through here, so the same
value never disagrees with itself across two code paths.

One decimal is the app's working precision: the trim UI snaps selections to
``SNAP_INTERVAL_SECONDS`` (0.5 s, see app/static/js/utils.js), so a legitimate
selection never carries more than one decimal place either.
"""

from __future__ import annotations

import math
from numbers import Real
from typing import Any, Final

SECONDS_DECIMALS: Final[int] = 1
# EN DASH, the same placeholder EMPTY_VALUE stands for in the browser code.
UNKNOWN_VALUE: Final[str] = "–"


def round_seconds(value: Any) -> float | None:
    """Round a seconds value to at most one decimal place.

    Returns ``None`` for anything that is not a usable number - a missing
    column, a bool, or a NaN/infinite ffprobe reading.
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        return None

    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        return None

    return round(numeric_value, SECONDS_DECIMALS)


def format_clock(value: Any) -> str:
    """Render a seconds value as a clock time: ``4:21:30`` or ``21:30``.

    Mirrors ``formatDuration()`` in app/static/js/utils.js so a duration reads
    the same whether the server rendered it or the browser did. Returns the
    en dash both surfaces use for "unknown".
    """
    rounded = round_seconds(value)
    if rounded is None or rounded < 0:
        return UNKNOWN_VALUE

    total_seconds = int(rounded)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_seconds(value: Any) -> str:
    """Render a seconds value, keeping the decimal place only when it has one.

    ``213.4`` becomes ``"213.4"``, ``213.0`` becomes ``"213"``.
    """
    rounded = round_seconds(value)
    if rounded is None:
        return "?"
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{SECONDS_DECIMALS}f}"
