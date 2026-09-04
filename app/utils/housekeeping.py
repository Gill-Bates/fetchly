#!/usr/bin/env python3
#
# app/utils/housekeeping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cleanup of job artifacts on the filesystem."""

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from time import time

from .fs import resolve_within_root

logger = logging.getLogger(__name__)

# Callable(keep_days: int) -> expired job IDs.
type ExpiredJobIdsFunc = Callable[[int], list[str]]

# Callable(job_id: str) -> True if the job exists in the database.
type JobExistsFunc = Callable[[str], bool]

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)
_ORPHAN_DIR_GRACE_PERIOD_SECONDS = 900
_THUMBNAIL_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _is_job_uuid(name: str) -> bool:
    return _UUID_RE.fullmatch(name) is not None


def cleanup_thumbnail_cache(cache_dir: Path, *, now: float | None = None) -> int:
    """Remove thumbnail cache entries older than the configured TTL."""
    if not cache_dir.exists():
        return 0

    current_time = time() if now is None else now
    removed = 0
    try:
        entries = tuple(cache_dir.iterdir())
    except OSError as exc:
        logger.warning("Unable to scan thumbnail cache %s: %s", cache_dir, exc)
        return 0

    for path in entries:
        if not path.is_file() and not path.is_symlink():
            continue
        try:
            if current_time - path.stat().st_mtime <= _THUMBNAIL_CACHE_MAX_AGE_SECONDS:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Unable to clean thumbnail cache file %s: %s", path, exc)

    if removed:
        logger.info("Thumbnail cache cleanup: removed %d expired file(s)", removed)
    return removed


def cleanup_job_directory(job_id: str, data_dir: Path) -> bool:
    """Delete a job's download directory.

    True when it was deleted or already absent; False when ``job_id`` is
    invalid, the path is unsafe, or deletion fails.
    """
    if not _is_job_uuid(job_id):
        logger.error("Refusing to delete invalid job_id: %r", job_id)
        return False

    try:
        job_dir = resolve_within_root(Path(job_id), data_dir, allow_symlink=False)
    except OSError as exc:
        logger.warning("Failed to resolve data directory %s: %s", data_dir, exc)
        return False
    except ValueError as exc:
        logger.error("Refusing to delete unsafe job path for %r: %s", job_id, exc)
        return False

    if not job_dir.exists():
        return True

    if not job_dir.is_dir():
        logger.warning("Job path is not a directory: %s", job_dir)
        return False

    try:
        shutil.rmtree(job_dir)
        logger.debug("Deleted job directory: %s", job_dir)
        return True
    except OSError as exc:
        logger.warning("Failed to delete directory %s: %s", job_dir, exc)
        return False


def cleanup_expired_jobs(
    keep_days: int,
    data_dir: Path,
    expired_job_ids_func: ExpiredJobIdsFunc,
) -> tuple[int, int]:
    """Clean filesystem artifacts for expired jobs without deleting DB rows.

    Returns ``(expired_jobs_found, filesystem_cleanup_ok)`` - the second count
    includes directories that were already absent. Raises TypeError/ValueError
    for a non-int or negative ``keep_days``.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    expired_ids = expired_job_ids_func(keep_days)
    if not expired_ids:
        return (0, 0)

    expired_jobs_found = len(expired_ids)

    valid_ids = [job_id for job_id in expired_ids if _is_job_uuid(job_id)]
    invalid_count = len(expired_ids) - len(valid_ids)
    if invalid_count:
        logger.warning(
            "Housekeeping: expiry selector returned %d invalid job ID(s); processing %d valid ID(s)",
            invalid_count,
            len(valid_ids),
        )

    try:
        data_dir_resolved = data_dir.resolve()
    except OSError as exc:
        logger.warning("Failed to resolve data directory %s: %s", data_dir, exc)
        return (expired_jobs_found, 0)

    dirs_ok = 0
    for job_id in valid_ids:
        if cleanup_job_directory(job_id, data_dir_resolved):
            dirs_ok += 1

    logger.info(
        "Housekeeping: %d expired job(s) found older than %d days; filesystem cleanup succeeded for %d",
        expired_jobs_found,
        keep_days,
        dirs_ok,
    )

    return (expired_jobs_found, dirs_ok)


def cleanup_orphaned_directories(
    data_dir: Path,
    job_exists_func: JobExistsFunc,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Delete (or, with ``dry_run``, just report) UUID directories with no
    matching DB record. Returns every such name found, deleted or not.
    """
    orphans: list[str] = []
    cleaned = 0

    if not data_dir.exists():
        return orphans

    try:
        data_dir_resolved = data_dir.resolve()
    except OSError as exc:
        logger.warning("Failed to resolve data directory %s: %s", data_dir, exc)
        return orphans

    now = time()

    try:
        for entry in data_dir_resolved.iterdir():
            if not entry.is_dir() or not _is_job_uuid(entry.name):
                continue
            try:
                if (now - entry.stat().st_mtime) < _ORPHAN_DIR_GRACE_PERIOD_SECONDS:
                    continue
            except OSError as exc:
                logger.debug("Skipping orphan candidate %s due to stat failure: %s", entry, exc)
                continue

            name = entry.name
            if job_exists_func(name):
                continue

            if dry_run:
                orphans.append(name)
                continue

            # Re-check immediately before deletion to avoid reporting a job
            # that appeared after the first database lookup.
            if job_exists_func(name):
                logger.debug(
                    "Skipping orphan cleanup for %s because it appeared in the DB before deletion",
                    name,
                )
                continue

            orphans.append(name)
            if cleanup_job_directory(name, data_dir_resolved):
                cleaned += 1
                logger.info("Cleaned orphaned directory: %s", name)
    except OSError as exc:
        logger.warning("Unable to scan data directory %s: %s", data_dir_resolved, exc)

    if orphans:
        if dry_run:
            logger.info("Found %d orphaned directories (dry run, not deleted)", len(orphans))
        else:
            logger.info(
                "Orphan cleanup: %d found, %d deleted, %d failed or skipped",
                len(orphans),
                cleaned,
                len(orphans) - cleaned,
            )

    return orphans
