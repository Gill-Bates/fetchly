#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import os
import re
import sys
from typing import TextIO

import uvicorn

from app.utils.banner import print_banner_once


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive data from the final log line."""

    SENSITIVE_PATTERNS = (
        (re.compile(r"\b(auth_token|tubeyou_csrf|tubeyou_session|session|token)\b=([^;\s]+)", re.IGNORECASE), r"\1=***REDACTED***"),
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


def _env_csv(name: str, default_value: str) -> str:
    raw = str(os.environ.get(name, default_value)).strip()
    return raw or default_value


def _should_use_colors(stream: TextIO) -> bool:
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
    TIMESTAMP_COLOR = "\x1b[90m"  # dark gray
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
        message = super().format(record)
        if not self.use_colors:
            return message

        timestamp = getattr(record, "asctime", None)
        if timestamp and message.startswith(timestamp):
            colored_timestamp = f"{self.TIMESTAMP_COLOR}{timestamp}{self.RESET}"
            message = f"{colored_timestamp}{message[len(timestamp):]}"

        color = self.LEVEL_COLORS.get(record.levelno)
        if not color:
            return message

        levelname = record.levelname
        colored_levelname = f"{color}{levelname}{self.RESET}"
        marker = f" - {levelname} - "
        colored_marker = f" - {colored_levelname} - "
        if marker in message:
            return message.replace(marker, colored_marker, 1)
        return message.replace(levelname, colored_levelname, 1)


if __name__ == "__main__":
    print_banner_once()

    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    log_level_upper = log_level.upper()
    reload_enabled = _env_truthy(os.environ.get("UVICORN_RELOAD"))
    use_colors = _should_use_colors(sys.stdout)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    graceful_shutdown_timeout = float(os.environ.get("UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN", "2.0"))
    forwarded_allow_ips = _env_csv(
        "FORWARDED_ALLOW_IPS",
        "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7",
    )

    stream_handler = {
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "formatter": "redacted",
    }

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
            "default": dict(stream_handler),
            "access": dict(stream_handler),
        },
        "root": {
            "handlers": ["default"],
            "level": log_level_upper,
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": log_level_upper,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": log_level_upper,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": log_level_upper,
                "propagate": False,
            },
        },
    }

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        timeout_graceful_shutdown=graceful_shutdown_timeout,
        log_config=log_config,
    )
