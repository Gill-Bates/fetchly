#!/usr/bin/env python3
#
# app/utils/housekeeping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Centralized housekeeping utilities for tubeyou.

This module provides functions for cleaning up job artifacts,
including database records and filesystem directories.
"""

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Callable(keep_days: int) -> deleted job IDs.
type PurgeDbFunc = Callable[[int], list[str]]

# Callable(job_id: str) -> True if the job exists in the database.
type JobExistsFunc = Callable[[str], bool]

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _is_job_uuid(name: str) -> bool:
    """Return True if the directory name uses canonical UUID formatting."""
    return _UUID_RE.fullmatch(name) is not None


def cleanup_job_directory(job_id: str, data_dir: Path) -> bool:
    """Delete a job's download directory and all its artifacts.

    Args:
        job_id: The job UUID.
        data_dir: Base data directory containing job folders.

    Returns:
        True if the directory was deleted or already absent, False on error.
    """
    if not _is_job_uuid(job_id):
        logger.error("Refusing to delete invalid job_id: %r", job_id)
        return False

    data_dir_resolved = data_dir.resolve()
    job_dir = (data_dir_resolved / job_id).resolve()

    if not job_dir.is_relative_to(data_dir_resolved):
        logger.error(
            "Refusing to delete %s because it escapes data directory %s",
            job_dir,
            data_dir_resolved,
        )
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
    purge_db_func: PurgeDbFunc,
) -> tuple[int, int]:
    """Delete expired jobs from the database and clean their filesystem artifacts.

    Args:
        keep_days: Number of days to retain completed jobs.
        data_dir: Base data directory containing job folders.
        purge_db_func: Callable that purges DB records and returns deleted IDs.

    Returns:
        Tuple of `(db_records_deleted, filesystem_cleanup_ok)`.
        The second count includes job IDs for which `cleanup_job_directory()`
        returned True, including directories that were already absent.

    Raises:
        ValueError: If keep_days is negative.
    """
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    # Purge from database first
    deleted_ids = purge_db_func(keep_days)
    if not deleted_ids:
        return (0, 0)

    # Clean up filesystem artifacts
    dirs_ok = 0
    for job_id in deleted_ids:
        if cleanup_job_directory(job_id, data_dir):
            dirs_ok += 1

    logger.info(
        "Housekeeping: %d job(s) older than %d days removed (%d filesystem cleanup outcomes ok)",
        len(deleted_ids),
        keep_days,
        dirs_ok,
    )

    return (len(deleted_ids), dirs_ok)


def cleanup_orphaned_directories(
    data_dir: Path,
    job_exists_func: JobExistsFunc,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Find orphaned job directories without matching DB records.

    Args:
        data_dir: Base data directory containing job folders.
        job_exists_func: Function that checks if a job_id exists in DB.
        dry_run: If True, only report orphans without deleting them.

    Returns:
        Names of all UUID directories without a matching DB record.
        Returned regardless of whether deletion succeeded or was skipped via
        `dry_run=True`. Callers cannot distinguish deleted from failed entries
        from the return value alone.
    """
    orphans: list[str] = []
    cleaned = 0

    if not data_dir.exists():
        return orphans

    # Snapshot candidates first to reduce races with concurrent directory creation.
    candidates = [
        entry
        for entry in data_dir.iterdir()
        if entry.is_dir() and _is_job_uuid(entry.name)
    ]

    for entry in candidates:
        name = entry.name
        if not job_exists_func(name):
            orphans.append(name)
            if dry_run:
                continue

            # Re-check right before deletion to narrow the race window with
            # concurrent job creation after the snapshot/orphan decision.
            if job_exists_func(name):
                logger.debug(
                    "Skipping orphan cleanup for %s because it appeared in the DB before deletion",
                    name,
                )
                continue

            if cleanup_job_directory(name, data_dir):
                cleaned += 1
                logger.info("Cleaned orphaned directory: %s", name)

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
