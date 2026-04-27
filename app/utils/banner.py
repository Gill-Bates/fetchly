#!/usr/bin/env python3
#
# app/utils/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup banner for tubeyou."""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import time

from .version import BUILD_INFO, VERSION

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
        f"Use Youtube with ease!  v{VERSION} ({build_short})",
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
    

def print_banner_once() -> None:
    """Print startup banner at most once per process tree.

    Uses a file lock so that only one worker prints the banner,
    even when running with multiple uvicorn workers.
    """
    ppid = str(os.getppid())

    try:
        fd = os.open(_BANNER_LOCK_FILE, os.O_CREAT | os.O_RDWR)
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

            # Skip if same parent AND lock file is recent (< 30s = same startup, different worker)
            if stored_ppid == ppid and (now - stored_ts) < 30:
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
    except OSError:
        # Fallback: just print (better than crashing)
        print_banner()