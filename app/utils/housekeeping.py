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

from ..db import delete_expired_job_if_eligible
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
_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _is_job_uuid(name: str) -> bool:
    return _UUID_RE.fullmatch(name) is not None


def _cleanup_cache_dir(cache_dir: Path, label: str, *, now: float | None) -> int:
    """Remove files in ``cache_dir`` whose mtime is past the cache TTL."""
    if not cache_dir.exists():
        return 0

    current_time = time() if now is None else now
    removed = 0
    try:
        entries = tuple(cache_dir.iterdir())
    except OSError as exc:
        logger.warning("Unable to scan %s cache %s: %s", label, cache_dir, exc)
        return 0

    for path in entries:
        if not path.is_file() and not path.is_symlink():
            continue
        try:
            if current_time - path.stat().st_mtime <= _CACHE_MAX_AGE_SECONDS:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Unable to clean %s cache file %s: %s", label, path, exc)

    if removed:
        logger.info("%s cache cleanup: removed %d expired file(s)", label.capitalize(), removed)
    return removed


def cleanup_thumbnail_cache(cache_dir: Path, *, now: float | None = None) -> int:
    """Remove thumbnail cache entries older than the configured TTL."""
    return _cleanup_cache_dir(cache_dir, "thumbnail", now=now)


def cleanup_watermark_cache(cache_dir: Path, *, now: float | None = None) -> int:
    """Remove rendered watermark badges (and stale logo snapshots) past the TTL.

    Badges are keyed by size, hostname and logo, so changing any of them leaves
    the old entries behind; a still-needed badge is simply re-rendered.
    """
    return _cleanup_cache_dir(cache_dir, "watermark", now=now)


def cleanup_job_directory(job_id: str, jobs_root: Path) -> bool:
    """Delete a job's download directory under ``jobs_root`` (``DATA_DIR/jobs``).

    True when it was deleted or already absent; False when ``job_id`` is
    invalid, the path is unsafe, or deletion fails.
    """
    if not _is_job_uuid(job_id):
        logger.error("Refusing to delete invalid job_id: %r", job_id)
        return False

    try:
        job_dir = resolve_within_root(Path(job_id), jobs_root, allow_symlink=False)
    except OSError as exc:
        logger.warning("Failed to resolve jobs directory %s: %s", jobs_root, exc)
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
    jobs_root: Path,
    expired_job_ids_func: ExpiredJobIdsFunc,
) -> list[str]:
    """Delete files and database rows for expired jobs; return deleted IDs.

    The selector supplies candidates only. Each candidate is rechecked in an
    immediate SQLite transaction, which remains open through the directory
    deletion and matching row/link deletion. A retry that committed first is
    therefore skipped; a retry that arrives later cannot succeed after
    retention has claimed that job. A directory that is already absent counts
    as cleaned. Failed directory deletion leaves the row and links for a later
    sweep. Raises TypeError/ValueError for invalid days.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    expired_ids = expired_job_ids_func(keep_days)
    if not expired_ids:
        return []

    valid_ids = [job_id for job_id in expired_ids if _is_job_uuid(job_id)]
    invalid_count = len(expired_ids) - len(valid_ids)
    if invalid_count:
        logger.warning(
            "Housekeeping: expiry selector returned %d invalid job ID(s); processing %d valid ID(s)",
            invalid_count,
            len(valid_ids),
        )

    try:
        jobs_root_resolved = jobs_root.resolve()
    except OSError as exc:
        logger.warning("Failed to resolve jobs directory %s: %s", jobs_root, exc)
        return []

    cleaned: list[str] = []
    for job_id in valid_ids:
        deleted, links = delete_expired_job_if_eligible(
            job_id,
            keep_days,
            lambda job_id=job_id: cleanup_job_directory(job_id, jobs_root_resolved),
        )
        if deleted:
            cleaned.append(job_id)
            if links:
                logger.info("Housekeeping: removed %d share link(s) for expired job %s", links, job_id)

    logger.info(
        "Housekeeping: %d expired job(s) found older than %d days; cleanup succeeded for %d",
        len(expired_ids),
        keep_days,
        len(cleaned),
    )

    return cleaned


def cleanup_orphaned_directories(
    jobs_root: Path,
    job_exists_func: JobExistsFunc,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Delete (or, with ``dry_run``, just report) UUID directories under
    ``jobs_root`` (``DATA_DIR/jobs``) with no matching DB record. Returns every
    such name found, deleted or not.
    """
    orphans: list[str] = []
    cleaned = 0

    if not jobs_root.exists():
        return orphans

    try:
        jobs_root_resolved = jobs_root.resolve()
    except OSError as exc:
        logger.warning("Failed to resolve jobs directory %s: %s", jobs_root, exc)
        return orphans

    now = time()

    try:
        for entry in jobs_root_resolved.iterdir():
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
            if cleanup_job_directory(name, jobs_root_resolved):
                cleaned += 1
                logger.info("Cleaned orphaned directory: %s", name)
    except OSError as exc:
        logger.warning("Unable to scan jobs directory %s: %s", jobs_root_resolved, exc)

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
