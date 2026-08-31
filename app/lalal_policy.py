#!/usr/bin/env python3
#
# app/lalal_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared product limits for Lalal.ai processing."""

from typing import Final

LALAL_MAX_DURATION_SECONDS: Final[int] = 600
LALAL_MAX_DURATION_MINUTES: Final[int] = LALAL_MAX_DURATION_SECONDS // 60
