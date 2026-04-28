#!/usr/bin/env python3
#
# app/governor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Centralized resource governor for CPU detection, semaphore management,
# backpressure control, and worker scaling. Designed for small VPS under load.
#

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Final, TypedDict

logger = logging.getLogger(__name__)


_GOVERNOR_NOT_CONFIGURED_MSG: Final = (
    "Governor not configured. Call governor.configure() at application startup."
)
_MEMORY_CACHE_TTL_SECONDS: Final[float] = 5.0
_CGROUP_V1_UNLIMITED_HEURISTIC: Final[int] = 2 ** 60


def _read_cgroup_file(path: str) -> str | None:
    """Read a cgroup file, returning None if not accessible."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return None


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    """Parse an integer environment variable with explicit validation."""
    raw = os.environ.get(name)
    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc

    if min_value is not None and value < min_value:
        raise ValueError(f"Environment variable {name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"Environment variable {name} must be <= {max_value}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable with explicit validation."""
    raw = os.environ.get(name)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be a boolean value, got {raw!r}")


def _parse_cpuset(spec: str | None) -> int | None:
    """Parse a cpuset spec like '0-3,5,7' into CPU count."""
    if not spec:
        return None
    total = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                total += int(end) - int(start) + 1
            else:
                int(part)
                total += 1
        except ValueError:
            logger.warning("Ignoring invalid cpuset fragment: %r", part)
    return total or None


def detect_effective_cpus() -> float:
    """
    Detect effective CPU count respecting Docker/cgroup limits.
    
    Checks (in order of priority):
    1. sched_getaffinity (cpuset binding)
    2. cgroup v2 cpuset.cpus.effective
    3. cgroup v1 cpuset.cpus
    4. cgroup v2 cpu.max (quota/period)
    5. cgroup v1 cpu.cfs_quota_us/cpu.cfs_period_us
    6. os.cpu_count() as fallback
    
    Returns the minimum of all applicable limits (at least 0.5).
    """
    limits: list[float] = []
    
    # 1. sched_getaffinity - CPU pinning via cpuset
    try:
        limits.append(float(len(os.sched_getaffinity(0))))
    except (AttributeError, OSError):
        pass
    
    # 2. cgroup v2 cpuset
    cpuset = _parse_cpuset(_read_cgroup_file("/sys/fs/cgroup/cpuset.cpus.effective"))
    if cpuset is None:
        # 3. cgroup v1 cpuset
        cpuset = _parse_cpuset(_read_cgroup_file("/sys/fs/cgroup/cpuset/cpuset.cpus"))
    if cpuset is not None:
        limits.append(float(cpuset))
    
    # 4. cgroup v2 cpu.max (quota period format: "100000 100000" or "max 100000")
    cpu_max = _read_cgroup_file("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) >= 2 and parts[0] != "max":
            try:
                limits.append(float(parts[0]) / float(parts[1]))
            except (ValueError, ZeroDivisionError):
                pass
    else:
        # 5. cgroup v1 quota/period
        quota = _read_cgroup_file("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _read_cgroup_file("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota and period:
            try:
                quota_val, period_val = int(quota), int(period)
                if quota_val > 0 and period_val > 0:
                    limits.append(quota_val / period_val)
            except ValueError:
                pass
    
    # 6. Fallback to os.cpu_count()
    if not limits:
        fallback = os.cpu_count() or 1
        limits.append(float(fallback))
    
    effective = max(0.5, min(limits))
    logger.debug("CPU detection: limits=%s, effective=%.2f", limits, effective)
    return effective


@dataclass(slots=True)
class GovernorConfig:
    """Configuration for the Governor."""
    
    # Worker counts (0 = auto-detect)
    worker_count: int = 0
    
    # Queue settings
    queue_maxsize: int = 0  # 0 = auto (2x workers)
    
    # Semaphore limits (0 = auto-detect)
    cpu_semaphore_limit: int = 0
    io_semaphore_limit: int = 0
    transcode_semaphore_limit: int = 0
    
    # Memory threshold in megabytes. Stop accepting jobs below this value.
    memory_threshold_mb: int = 256
    
    # Backpressure settings
    enable_backpressure: bool = True
    
    @classmethod
    def from_env(cls) -> GovernorConfig:
        """Create config from environment variables."""
        return cls(
            worker_count=_env_int("WORKER_COUNT", 0, min_value=0),
            queue_maxsize=_env_int("WORKER_QUEUE_MAXSIZE", 0, min_value=0),
            cpu_semaphore_limit=_env_int("CPU_SEMAPHORE_LIMIT", 0, min_value=0),
            io_semaphore_limit=_env_int("IO_SEMAPHORE_LIMIT", 0, min_value=0),
            transcode_semaphore_limit=_env_int("TRANSCODE_SEMAPHORE_LIMIT", 0, min_value=0),
            memory_threshold_mb=_env_int("MEMORY_THRESHOLD_MB", 256, min_value=0),
            enable_backpressure=_env_bool("ENABLE_BACKPRESSURE", True),
        )


@dataclass(slots=True)
class ResourceLimits:
    """Calculated resource limits based on system detection."""
    
    effective_cpus: float
    worker_count: int
    queue_maxsize: int
    cpu_limit: int
    io_limit: int
    transcode_limit: int


class GovernorStatus(TypedDict):
    effective_cpus: float
    worker_count: int
    queue_maxsize: int
    cpu_limit: int
    io_limit: int
    transcode_limit: int
    memory_available_mb: int
    memory_threshold_mb: int
    memory_backpressure_triggered: bool
    backpressure_enabled: bool
    can_accept_job: bool


class Governor:
    """
    Central resource governor for managing system resources.
    
    Provides:
    - Auto-detection of available CPUs (Docker cgroup-aware)
    - Semaphores for different workload types (CPU, IO, transcoding)
    - Queue configuration with backpressure
    - Worker count calculation
    - Memory monitoring
    
    Usage:
        from app.governor import governor
        
        # Configure at startup
        governor.configure()
        
        # Get worker count
        n_workers = governor.worker_count
        
        # Use semaphores for rate limiting
        async with governor.transcode_semaphore:
            await transcode_video()
        
        # Sync semaphore for threading
        with governor.cpu_semaphore_sync:
            cpu_intensive_work()
    """
    
    def __init__(self) -> None:
        self._config: GovernorConfig | None = None
        self._limits: ResourceLimits | None = None
        self._lock = threading.Lock()
        
        # Semaphores are initialized eagerly in configure()
        self._cpu_sem: asyncio.Semaphore | None = None
        self._io_sem: asyncio.Semaphore | None = None
        self._transcode_sem: asyncio.Semaphore | None = None
        
        # Sync semaphores (threading)
        self._cpu_sem_sync: threading.Semaphore | None = None
        self._io_sem_sync: threading.Semaphore | None = None
        self._transcode_sem_sync: threading.Semaphore | None = None

        # Short-lived memory cache to keep status checks cheap
        self._memory_cache: tuple[float, int] | None = None
        self._memory_refreshing = False
        
        self._configured = False
    
    def configure(self, config: GovernorConfig | None = None) -> None:
        """
        Configure the governor with resource limits.
        
        Call this once at application startup. If config is None,
        auto-detects from environment and system.
        """
        with self._lock:
            if self._configured:
                logger.debug("Governor already configured, skipping")
                return
            
            self._config = config or GovernorConfig.from_env()
            effective_cpus = detect_effective_cpus()
            
            # Calculate worker count
            if self._config.worker_count > 0:
                worker_count = self._config.worker_count
            else:
                # Auto: 2 workers per CPU, min 1, max 8
                worker_count = max(1, min(8, math.ceil(effective_cpus * 2)))
            
            # Calculate queue maxsize
            if self._config.queue_maxsize > 0:
                queue_maxsize = self._config.queue_maxsize
            elif self._config.enable_backpressure:
                # Auto: 2x workers for backpressure
                queue_maxsize = worker_count * 2
            else:
                queue_maxsize = 0  # Unlimited
            
            # Calculate semaphore limits
            cpu_limit = self._config.cpu_semaphore_limit or max(1, math.ceil(effective_cpus))
            io_limit = self._config.io_semaphore_limit or max(2, math.ceil(effective_cpus * 4))
            transcode_limit = self._config.transcode_semaphore_limit or max(1, min(2, math.ceil(effective_cpus)))
            
            self._limits = ResourceLimits(
                effective_cpus=effective_cpus,
                worker_count=worker_count,
                queue_maxsize=queue_maxsize,
                cpu_limit=cpu_limit,
                io_limit=io_limit,
                transcode_limit=transcode_limit,
            )
            
            # Initialize semaphores eagerly to avoid race conditions.
            self._cpu_sem = asyncio.Semaphore(cpu_limit)
            self._io_sem = asyncio.Semaphore(io_limit)
            self._transcode_sem = asyncio.Semaphore(transcode_limit)
            self._cpu_sem_sync = threading.Semaphore(cpu_limit)
            self._io_sem_sync = threading.Semaphore(io_limit)
            self._transcode_sem_sync = threading.Semaphore(transcode_limit)
            
            self._configured = True
            
            logger.info(
                "Governor configured: cpus=%.2f, workers=%d, queue=%d, "
                "cpu_sem=%d, io_sem=%d, transcode_sem=%d",
                effective_cpus,
                worker_count,
                queue_maxsize,
                cpu_limit,
                io_limit,
                transcode_limit,
            )
    
    @property
    def _limits_checked(self) -> ResourceLimits:
        if self._limits is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._limits

    @property
    def _config_checked(self) -> GovernorConfig:
        if self._config is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._config

    def _require_configured(self) -> tuple[GovernorConfig, ResourceLimits]:
        """Return the configured governor state or raise if not initialized."""
        config = self._config
        limits = self._limits
        if config is None or limits is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return config, limits
    
    @property
    def effective_cpus(self) -> float:
        """Effective CPU count (Docker cgroup-aware)."""
        return self._limits_checked.effective_cpus
    
    @property
    def worker_count(self) -> int:
        """Recommended worker count."""
        return self._limits_checked.worker_count
    
    @property
    def queue_maxsize(self) -> int:
        """Recommended queue maxsize for backpressure."""
        return self._limits_checked.queue_maxsize

    @property
    def transcode_limit(self) -> int:
        """Recommended concurrency limit for transcoding/analysis work."""
        return self._limits_checked.transcode_limit
    
    @property
    def cpu_semaphore(self) -> asyncio.Semaphore:
        """Async semaphore for CPU-intensive operations."""
        if self._cpu_sem is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._cpu_sem
    
    @property
    def io_semaphore(self) -> asyncio.Semaphore:
        """Async semaphore for IO operations."""
        if self._io_sem is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._io_sem
    
    @property
    def transcode_semaphore(self) -> asyncio.Semaphore:
        """Async semaphore for transcoding operations."""
        if self._transcode_sem is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._transcode_sem
    
    @property
    def cpu_semaphore_sync(self) -> threading.Semaphore:
        """Threading semaphore for CPU-intensive operations."""
        if self._cpu_sem_sync is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._cpu_sem_sync

    @property
    def io_semaphore_sync(self) -> threading.Semaphore:
        """Threading semaphore for IO operations."""
        if self._io_sem_sync is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._io_sem_sync
    
    @property
    def transcode_semaphore_sync(self) -> threading.Semaphore:
        """Threading semaphore for transcoding operations."""
        if self._transcode_sem_sync is None:
            raise RuntimeError(_GOVERNOR_NOT_CONFIGURED_MSG)
        return self._transcode_sem_sync

    def _read_memory_available_mb_uncached(self) -> int:
        """Read available memory in MB from cgroup or /proc without caching."""
        result = -1

        try:
            # cgroup v2
            mem_current = _read_cgroup_file("/sys/fs/cgroup/memory.current")
            mem_max = _read_cgroup_file("/sys/fs/cgroup/memory.max")
            if mem_current and mem_max and mem_max != "max":
                current = int(mem_current)
                maximum = int(mem_max)
                return max(0, (maximum - current) // (1024 * 1024))
        except (ValueError, TypeError):
            pass

        try:
            # cgroup v1
            mem_usage = _read_cgroup_file("/sys/fs/cgroup/memory/memory.usage_in_bytes")
            mem_limit = _read_cgroup_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
            if mem_usage and mem_limit:
                usage = int(mem_usage)
                limit = int(mem_limit)
                # Very high limit means no cgroup limit.
                if limit < _CGROUP_V1_UNLIMITED_HEURISTIC:
                    return max(0, (limit - usage) // (1024 * 1024))
        except (ValueError, TypeError):
            pass

        try:
            # Fallback: /proc/meminfo
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            for line in meminfo.splitlines():
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
        except (OSError, ValueError, IndexError):
            pass

        return result

    def _store_memory_cache(self, value: int) -> None:
        with self._lock:
            self._memory_cache = (monotonic(), value)
            self._memory_refreshing = False

    def _refresh_memory_cache_background(self) -> None:
        """Refresh the memory cache asynchronously after it expires."""
        try:
            value = self._read_memory_available_mb_uncached()
        except Exception:
            logger.exception("Failed to refresh memory cache")
            # Reset refresh flag so the next caller can trigger a new attempt.
            with self._lock:
                self._memory_refreshing = False
            return

        self._store_memory_cache(value)
    
    def get_memory_available_mb(self) -> int:
        """Get available memory in MB (cgroup-aware).

        Cache hits are lock-protected and return immediately. On cache miss
        (first call after startup), a synchronous file read is performed.
        When the cache expires and a previous value exists, this returns the
        stale value immediately and refreshes the cache in a background thread
        to avoid blocking async callers.
        """
        now = monotonic()
        with self._lock:
            cached = self._memory_cache
            if cached is not None:
                cached_at, cached_value = cached
                if now - cached_at < _MEMORY_CACHE_TTL_SECONDS:
                    return cached_value
                if not self._memory_refreshing:
                    self._memory_refreshing = True
                    threading.Thread(
                        target=self._refresh_memory_cache_background,
                        daemon=True,
                    ).start()
                    return cached_value

        result = self._read_memory_available_mb_uncached()
        self._store_memory_cache(result)
        return result

    def _memory_backpressure_state(self) -> tuple[int, int, bool]:
        """Return (available_mb, threshold_mb, is_triggered)."""
        config = self._config_checked
        mem_available = self.get_memory_available_mb()
        threshold = config.memory_threshold_mb
        triggered = config.enable_backpressure and mem_available >= 0 and mem_available < threshold
        return mem_available, threshold, triggered
    
    def can_accept_job(self) -> bool:
        """Check whether memory pressure allows accepting another job."""
        config, _ = self._require_configured()

        if not config.enable_backpressure:
            return True
        
        mem_available, threshold, triggered = self._memory_backpressure_state()
        if triggered:
            logger.warning(
                "Memory below threshold: %dMB available, %dMB required",
                mem_available,
                threshold,
            )
            return False
        
        return True
    
    def status(self) -> GovernorStatus:
        """Return current governor status without logging side effects."""
        config, limits = self._require_configured()
        mem_available, threshold, triggered = self._memory_backpressure_state()
        can_accept = True if not config.enable_backpressure else not triggered

        return {
            "effective_cpus": limits.effective_cpus,
            "worker_count": limits.worker_count,
            "queue_maxsize": limits.queue_maxsize,
            "cpu_limit": limits.cpu_limit,
            "io_limit": limits.io_limit,
            "transcode_limit": limits.transcode_limit,
            "memory_available_mb": mem_available,
            "memory_threshold_mb": threshold,
            "memory_backpressure_triggered": triggered,
            "backpressure_enabled": config.enable_backpressure,
            "can_accept_job": can_accept,
        }


# Global singleton
governor = Governor()


# Convenience exports
def configure(config: GovernorConfig | None = None) -> None:
    """Configure the global governor."""
    governor.configure(config)


def get_worker_count() -> int:
    """Get recommended worker count."""
    return governor.worker_count


def get_queue_maxsize() -> int:
    """Get recommended queue maxsize."""
    return governor.queue_maxsize
