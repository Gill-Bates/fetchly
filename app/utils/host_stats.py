#!/usr/bin/env python3
#
# app/utils/host_stats.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Lightweight host resource sampling for the Settings -> System panel.

Everything here is best-effort and Linux-first: fetchly ships as a Linux
container, so each metric reads straight from ``/proc`` (host-wide values,
which is what "Host resources" in the UI means) and degrades to ``None``
when the file or syscall is unavailable rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from pathlib import Path
from time import monotonic
from typing import TypedDict

logger = logging.getLogger(__name__)

# The short reading taken when there is no usable previous CPU sample.
_CPU_SAMPLE_INTERVAL_SECONDS = 0.15
# A stored sample older than this is treated as stale: dividing a spike into a
# multi-minute window would flatten it into nothing, so take a fresh reading.
_CPU_SAMPLE_MAX_AGE_SECONDS = 60.0

_cpu_lock = threading.Lock()
# (monotonic_ts, idle_jiffies, total_jiffies) from the previous /proc/stat read.
_last_cpu_sample: tuple[float, int, int] | None = None


class StorageStats(TypedDict):
    total: int
    used: int
    free: int
    percent: float


class CpuStats(TypedDict):
    percent: float
    cores: int | None


class MemoryStats(TypedDict):
    total: int
    used: int
    available: int
    percent: float


class UptimeStats(TypedDict):
    seconds: float
    text: str


class HostStats(TypedDict):
    storage: StorageStats | None
    cpu: CpuStats | None
    memory: MemoryStats | None
    uptime: UptimeStats | None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _storage_usage(path: Path) -> StorageStats | None:
    """Return disk usage for the filesystem holding the download directory."""
    probe = path
    # The data dir is created at startup, but fall back to the nearest existing
    # parent so a call during a brief window (or a misconfigured mount) still
    # reports the volume rather than nothing.
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        logger.debug("disk_usage(%s) failed: %s", probe, exc)
        return None

    percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


def _read_cpu_jiffies() -> tuple[int, int] | None:
    """Return (idle, total) aggregate CPU jiffies from the /proc/stat cpu line."""
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError as exc:
        logger.debug("Unable to read /proc/stat: %s", exc)
        return None

    if not first_line.startswith("cpu "):
        return None

    try:
        values = [int(token) for token in first_line.split()[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None

    # Fields: user nice system idle iowait irq softirq steal guest guest_nice.
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _cpu_percent_between(prev: tuple[int, int], curr: tuple[int, int]) -> float | None:
    idle_delta = curr[0] - prev[0]
    total_delta = curr[1] - prev[1]
    if total_delta <= 0:
        return None
    busy = 1.0 - (idle_delta / total_delta)
    return max(0.0, min(100.0, busy * 100.0))


async def _sample_cpu_percent() -> float | None:
    """Busy-CPU percentage since the previous call, cgroup-agnostic (host-wide).

    Warm path (a recent stored sample exists): returns immediately with the
    delta over the real gap between calls. Cold/stale path: takes one short
    inline reading.
    """
    global _last_cpu_sample

    current = _read_cpu_jiffies()
    if current is None:
        return None
    now = monotonic()

    with _cpu_lock:
        previous = _last_cpu_sample

    if previous is not None:
        age = now - previous[0]
        if _CPU_SAMPLE_INTERVAL_SECONDS <= age <= _CPU_SAMPLE_MAX_AGE_SECONDS:
            percent = _cpu_percent_between((previous[1], previous[2]), current)
            if percent is not None:
                with _cpu_lock:
                    _last_cpu_sample = (now, current[0], current[1])
                return percent

    await asyncio.sleep(_CPU_SAMPLE_INTERVAL_SECONDS)
    second = _read_cpu_jiffies()
    if second is None:
        return None
    with _cpu_lock:
        _last_cpu_sample = (monotonic(), second[0], second[1])
    return _cpu_percent_between(current, second)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _read_meminfo() -> dict[str, int]:
    """Parse /proc/meminfo into a {label: bytes} mapping."""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                label, _, rest = line.partition(":")
                fields = rest.split()
                if not fields:
                    continue
                try:
                    info[label.strip()] = int(fields[0]) * 1024
                except ValueError:
                    continue
    except OSError as exc:
        logger.debug("Unable to read /proc/meminfo: %s", exc)
    return info


def _memory_usage() -> MemoryStats | None:
    info = _read_meminfo()
    total = info.get("MemTotal")
    if not total:
        return None

    available = info.get("MemAvailable")
    if available is None:
        # Pre-3.14 kernels: approximate with free + reclaimable caches.
        available = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
    available = max(0, min(available, total))

    used = total - available
    return {
        "total": total,
        "used": used,
        "available": available,
        "percent": round(used / total * 100, 1),
    }


# ---------------------------------------------------------------------------
# Uptime
# ---------------------------------------------------------------------------


def _read_uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            fields = handle.readline().split()
    except OSError as exc:
        logger.debug("Unable to read /proc/uptime: %s", exc)
        return None

    if not fields:
        return None
    try:
        return max(0.0, float(fields[0]))
    except ValueError:
        return None


def _format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


async def get_host_stats(data_dir: Path) -> HostStats:
    """Collect a one-shot snapshot of host storage, CPU, memory, and uptime."""
    cpu_percent = await _sample_cpu_percent()
    storage, memory, uptime_seconds = await asyncio.gather(
        asyncio.to_thread(_storage_usage, data_dir),
        asyncio.to_thread(_memory_usage),
        asyncio.to_thread(_read_uptime_seconds),
    )

    cpu: CpuStats | None = None
    if cpu_percent is not None:
        cpu = {"percent": round(cpu_percent, 1), "cores": os.cpu_count()}

    uptime: UptimeStats | None = None
    if uptime_seconds is not None:
        uptime = {"seconds": uptime_seconds, "text": _format_uptime(uptime_seconds)}

    return {"storage": storage, "cpu": cpu, "memory": memory, "uptime": uptime}
