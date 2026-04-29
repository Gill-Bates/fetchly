#!/usr/bin/env python3
#
# app/utils/housekeeping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Centralized housekeeping utilities for tubeyou.

This module provides functions for cleaning up job artifacts,
including database records and filesystem directories.
"""

from collections.abc import Callable
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

type PurgeDbFunc = Callable[[int], list[str]]
type JobExistsFunc = Callable[[str], bool]

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
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
        Tuple of `(db_records_deleted, directories_cleaned)`.
        The second count includes successful cleanup outcomes for returned job IDs,
        including directories that were already absent.

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
    dirs_deleted = 0
    for job_id in deleted_ids:
        if cleanup_job_directory(job_id, data_dir):
            dirs_deleted += 1

    logger.info(
        "Housekeeping: %d job(s) older than %d days removed (%d directories cleaned)",
        len(deleted_ids),
        keep_days,
        dirs_deleted,
    )

    return (len(deleted_ids), dirs_deleted)


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
        List of directory names identified as orphans. All detected orphans are
        returned regardless of `dry_run`. When `dry_run` is False, deletion is
        attempted for each orphan; failures are logged and still included.
    """
    orphans: list[str] = []

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
            if not dry_run and cleanup_job_directory(name, data_dir):
                logger.info("Cleaned orphaned directory: %s", name)

    if orphans:
        action = "Found" if dry_run else "Cleaned"
        logger.info("%s %d orphaned directories", action, len(orphans))

    return orphans
