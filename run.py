#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import os
import re
import sys
from copy import copy
from pathlib import Path
from typing import ClassVar, TextIO

import uvicorn

from app.common.rate_limit import validate_trusted_proxy_hosts
from app.utils.banner import print_banner_once
from app.utils.fs import get_data_dir

_VALID_LOG_LEVELS = frozenset(logging.getLevelNamesMapping())


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive data from the final log line."""

    SENSITIVE_PATTERNS = (
        (
            re.compile(
                r"(?P<key>\b(?:auth_token|fetchly_csrf|fetchly_session|session|token)\b=)(?P<value>[^&;\s]+)",
                re.IGNORECASE,
            ),
            r"\g<key>***REDACTED***",
        ),
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


def _env_str(name: str, default: str) -> str:
    """Read and normalize a comma-separated environment variable."""
    raw = os.environ.get(name, default)
    values = [part.strip() for part in raw.split(",") if part.strip()]
    return ",".join(values) if values else default


def _env_paths(name: str, default: list[str]) -> list[str]:
    """Read a comma-separated list of filesystem paths from the environment."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    values = [part.strip() for part in raw.split(",") if part.strip()]
    return values if values else default


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable with validation."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable with validation."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid float for {name}: {raw!r}") from exc


def _env_log_level(name: str, default: str = "INFO") -> str:
    """Read and validate a log level environment variable."""
    value = os.environ.get(name, default).strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise ValueError(f"Invalid {name}: {value!r} (valid: {', '.join(sorted(_VALID_LOG_LEVELS))})")
    return value


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
    return term not in {"", "dumb"}


class ColoredRedactingFormatter(RedactingFormatter):
    """Redacting formatter with optional ANSI level colors."""

    RESET = "\x1b[0m"
    TIMESTAMP_COLOR = "\x1b[90m"  # dark gray
    LEVEL_COLORS: ClassVar[dict[int, str]] = {
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

        colored = copy(record)
        if color := self.LEVEL_COLORS.get(record.levelno):
            colored.levelname = f"{color}{record.levelname}{self.RESET}"

        message = super().format(colored)

        timestamp = getattr(colored, "asctime", None)
        if timestamp and message.startswith(timestamp):
            message = f"{self.TIMESTAMP_COLOR}{timestamp}{self.RESET}{message[len(timestamp):]}"

        return message


def _build_log_config(*, log_level: str, use_colors: bool) -> dict[str, object]:
    """Build uvicorn log configuration dictionary."""
    stream_handler = {
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "formatter": "redacted",
    }
    return {
        "version": 1,
        # NOTE: False allows app loggers configured before uvicorn.run() to keep their handlers.
        # This means pre-existing handlers may log secrets unredacted. Set True for full control.
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
        "root": {"handlers": ["default"], "level": log_level},
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": log_level, "propagate": False},
        },
    }


def main() -> None:
    """Application entry point."""
    print_banner_once()

    log_level = _env_log_level("LOG_LEVEL", "INFO")
    reload_enabled = _env_truthy(os.environ.get("UVICORN_RELOAD"))
    reload_excludes = None
    reload_dirs = None
    if reload_enabled:
        reload_excludes = _env_paths(
            "UVICORN_RELOAD_EXCLUDES",
            [str(get_data_dir()), str(Path(__file__).resolve().parent / ".git")],
        )
        # uvicorn/watchfiles watches Path.cwd() when reload_dirs is unset, and
        # reload_excludes can only drop a watched dir that equals or contains
        # an excluded path - it cannot carve a subdirectory (like data/) back
        # out of a single top-level watch. Watching only the source dirs keeps
        # data/ and .git/ off the raw watcher instead of merely off the
        # post-filter, which is what actually stops the DEBUG log noise from
        # every job DB write.
        app_root = Path(__file__).resolve().parent
        reload_dirs = _env_paths(
            "UVICORN_RELOAD_DIRS",
            [str(app_root / "app"), str(app_root / "middleware")],
        )
    use_colors = _should_use_colors(sys.stdout)
    host = os.environ.get("HOST", "0.0.0.0")
    port = _env_int("PORT", 8000)
    graceful_shutdown_timeout = _env_float("UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN", 10.0)
    forwarded_allow_ips = _env_str(
        "FORWARDED_ALLOW_IPS",
        "127.0.0.1,::1",
    )
    forwarded_allow_ips = validate_trusted_proxy_hosts(forwarded_allow_ips)

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        reload_dirs=reload_dirs,
        reload_excludes=reload_excludes,
        # The application installs the single validated proxy-header middleware
        # itself. Keeping Uvicorn's duplicate middleware disabled preserves the
        # actual socket peer for the rate-limit trust decision.
        proxy_headers=False,
        forwarded_allow_ips=forwarded_allow_ips,
        timeout_graceful_shutdown=graceful_shutdown_timeout,
        log_config=_build_log_config(log_level=log_level, use_colors=use_colors),
    )


if __name__ == "__main__":
    main()
