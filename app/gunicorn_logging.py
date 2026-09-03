#!/usr/bin/env python3
#
# app/gunicorn_logging.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Gunicorn logging that matches Fetchly's Uvicorn log format."""

import logging

from gunicorn.glogging import Logger


class FetchlyGunicornLogger(Logger):
    """Format Gunicorn and Uvicorn worker logs consistently."""

    error_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    access_fmt = error_fmt
    datefmt = "%Y-%m-%d %H:%M:%S"

    def setup(self, cfg) -> None:
        super().setup(cfg)

        formatter = logging.Formatter(self.access_fmt, self.datefmt)
        for handler in self.access_log.handlers:
            if getattr(handler, "_gunicorn", False):
                handler.setFormatter(formatter)
