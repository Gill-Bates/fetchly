#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import os
import re
import sys

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


def _env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_use_colors(stream: object) -> bool:
    # Respect explicit opt-out first.
    if os.environ.get("NO_COLOR") is not None:
        return False

    # Support common explicit opt-in knobs used in terminals/containers.
    if _env_truthy(os.environ.get("FORCE_COLOR")):
        return True
    if _env_truthy(os.environ.get("CLICOLOR_FORCE")):
        return True

    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    if not is_tty:
        return False

    term = str(os.environ.get("TERM", "")).strip().lower()
    if term in {"", "dumb"}:
        return False

    return True


class ColoredRedactingFormatter(RedactingFormatter):
    """Redacting formatter with optional ANSI level colors."""

    RESET = "\x1b[0m"
    LEVEL_COLORS = {
        logging.DEBUG: "\x1b[36m",     # cyan
        logging.INFO: "\x1b[32m",      # green
        logging.WARNING: "\x1b[33m",   # yellow
        logging.ERROR: "\x1b[31m",     # red
        logging.CRITICAL: "\x1b[1;31m",  # bold red
    }

    def __init__(self, *args, use_colors: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_colors:
            return super().format(record)

        original_levelname = record.levelname
        color = self.LEVEL_COLORS.get(record.levelno)
        if color:
            record.levelname = f"{color}{original_levelname}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


if __name__ == "__main__":
    print_banner_once()

    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    reload_enabled = _env_truthy(os.environ.get("UVICORN_RELOAD"))
    use_colors = _should_use_colors(sys.stdout)

    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "redacted": {
                "()": ColoredRedactingFormatter,
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": use_colors,
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
