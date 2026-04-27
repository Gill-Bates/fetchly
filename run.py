#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import os
import re

import uvicorn

from app.utils.banner import print_banner_once


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive data from the final log line."""

    SENSITIVE_PATTERNS = (
        (re.compile(r"(auth_token|tubeyou_csrf|tubeyou_session|session|token)=([^;\s]+)", re.IGNORECASE), r"\1=***REDACTED***"),
        (re.compile(r"(authorization:\s*)(bearer\s+)?([^\s]+)", re.IGNORECASE), r"\1\2***REDACTED***"),
        (re.compile(r"(x-api-key:\s*)([^\s]+)", re.IGNORECASE), r"\1***REDACTED***"),
    )

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            message = pattern.sub(replacement, message)
        return message


if __name__ == "__main__":
    print_banner_once()

    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    reload_enabled = os.environ.get("UVICORN_RELOAD", "false").lower() in {"1", "true", "yes", "on"}

    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "redacted": {
                "()": RedactingFormatter,
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "redacted",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "access": {
                "formatter": "redacted",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "handlers": ["default"],
            "level": log_level.upper(),
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": log_level.upper(),
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": log_level.upper(),
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": log_level.upper(),
                "propagate": False,
            },
        },
    }

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload_enabled,
        log_config=log_config,
    )
