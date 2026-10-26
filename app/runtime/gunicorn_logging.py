#!/usr/bin/env python3
#
# app/runtime/gunicorn_logging.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Gunicorn logging that matches Fetchly's Uvicorn log format."""

import logging
import time

from gunicorn.glogging import Logger


class UtcFormatter(logging.Formatter):
    """Formatter whose ``%(asctime)s`` is UTC, independent of the host ``TZ``."""

    converter = time.gmtime


class FetchlyGunicornLogger(Logger):
    """Format Gunicorn and Uvicorn worker logs consistently, timestamps in UTC."""

    error_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    access_fmt = error_fmt
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    def setup(self, cfg) -> None:
        super().setup(cfg)

        formatter = UtcFormatter(self.access_fmt, self.datefmt)
        for target in (self.error_log, self.access_log):
            for handler in target.handlers:
                if getattr(handler, "_gunicorn", False):
                    handler.setFormatter(formatter)
