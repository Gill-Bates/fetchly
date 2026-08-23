#!/usr/bin/env python3
#
# app/utils/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup banner for tubeyou."""

from __future__ import annotations

import fcntl
import logging
import os
import stat
import sys
import tempfile
import time

from .version import BUILD_INFO, VERSION

logger = logging.getLogger(__name__)

_BANNER_LOCK_FILE = os.path.join(tempfile.gettempdir(), "tubeyou_banner.lock")

def _block_width(text: str) -> int:
    return max((len(line) for line in text.splitlines()), default=0)


def print_banner() -> None:
    """Print the tubeyou startup banner."""
    build_short = BUILD_INFO[:7] if BUILD_INFO else "dev"

    ascii_art = r"""
 _         _                            
| |_ _   _| |__   ___ _   _  ___  _   _ 
| __| | | | '_ \ / _ \ | | |/ _ \| | | |
| |_| |_| | |_) |  __/ |_| | (_) | |_| |
 \__|\__,_|_.__/ \___|\__, |\___/ \__,_|
                      |___/             
""".strip("\n")

    text_lines = [
        f"Use Youtube with ease! v{VERSION} ({build_short})",
        "(C) 2026 by Gill-Bates (https://github.com/Gill-Bates/tubeyou)",
    ]

    ascii_lines = ascii_art.splitlines()
    ascii_width = max((len(l) for l in ascii_lines), default=0)
    text_width = max((len(t) for t in text_lines), default=0)

    master_width = max(ascii_width, text_width)

    left_pad = max((master_width - ascii_width) // 2, 0)
    pad = " " * left_pad
    ascii_centered = "\n".join(pad + line for line in ascii_lines)

    text_centered = [t.center(master_width) for t in text_lines]

    banner = "\n" + "\n".join([ascii_centered, *text_centered]) + "\n"

    if sys.stdout.isatty():
        cyan = "\033[96m"
        reset = "\033[0m"
        sys.stdout.write(cyan + banner + reset + "\n")
    else:
        sys.stdout.write(banner + "\n")

    sys.stdout.flush()
    

def _open_lock_file() -> int:
    """Open the banner lock file, refusing anything but our own regular file.

    ``O_NOFOLLOW`` keeps a pre-planted symlink in the shared temp directory from
    redirecting the later ``ftruncate()`` onto an unrelated file.
    """
    fd = os.open(
        _BANNER_LOCK_FILE,
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError(f"Unsafe banner lock file: {_BANNER_LOCK_FILE}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def print_banner_once() -> None:
    """Print the startup banner once per parent PID within a 30-second window.

    A file lock plus the recorded parent PID keeps concurrent Gunicorn workers
    of the same startup from each printing the banner. The record is only
    honoured for 30 seconds - long enough to cover workers starting together,
    short enough that a later restart under the same parent (or a reused PID)
    prints again instead of being suppressed forever.
    """
    ppid = str(os.getppid())

    try:
        fd = _open_lock_file()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)

            content = os.read(fd, 64).decode("utf-8", errors="ignore").strip()

            # Parse stored "ppid:timestamp"
            stored_ppid, stored_ts = "", 0.0
            if ":" in content:
                parts = content.split(":")
                stored_ppid = parts[0]
                try:
                    stored_ts = float(parts[1])
                except (ValueError, IndexError):
                    pass

            now = time.time()

            # Skip if same parent AND lock file is recent (< 30s = same startup,
            # different worker). A negative delta means the clock moved backwards
            # and the record can no longer be trusted.
            elapsed = now - stored_ts
            if stored_ppid == ppid and 0 <= elapsed < 30:
                return

            # New startup -> print banner
            print_banner()

            # Write ppid:timestamp
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{ppid}:{now}".encode())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError as exc:
        # Fallback: still print, but surface *why* the lock path was skipped -
        # e.g. unsafe lock file, permission error - so it is not silently
        # invisible in ops when every worker ends up printing its own banner.
        logger.warning("Startup banner lock unavailable, printing without it: %s", exc)
        print_banner()