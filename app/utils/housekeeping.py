#!/usr/bin/env python3
#
# app/utils/housekeeping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Centralized housekeeping utilities for tubeyou.

This module provides functions for cleaning up job artifacts,
including database records and filesystem directories.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def cleanup_job_directory(job_id: str, data_dir: Path) -> bool:
    """Delete a job's download directory and all its artifacts.

    Args:
        job_id: The job UUID
        data_dir: Base data directory containing job folders

    Returns:
        True if directory was deleted or didn't exist, False on error
    """
    job_dir = data_dir / job_id
    if not job_dir.exists():
        return True

    if not job_dir.is_dir():
        logger.warning("Job path is not a directory: %s", job_dir)
        return False

    try:
        shutil.rmtree(job_dir)
        logger.debug("Deleted job directory: %s", job_dir)
        return True
    except OSError as e:
        logger.warning("Failed to delete directory %s: %s", job_dir, e)
        return False


def cleanup_expired_jobs(
    keep_days: int,
    data_dir: Path,
    purge_db_func: "Callable[[int], list[str]]",
) -> tuple[int, int]:
    """Delete expired jobs from database and filesystem.

    Args:
        keep_days: Number of days to retain completed jobs
        data_dir: Base data directory containing job folders
        purge_db_func: Function that purges DB records and returns deleted IDs

    Returns:
        Tuple of (jobs_deleted, directories_deleted)
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
    job_exists_func: "Callable[[str], bool]",
    *,
    dry_run: bool = False,
) -> list[str]:
    """Find and optionally delete directories without matching DB records.

    Args:
        data_dir: Base data directory containing job folders
        job_exists_func: Function that checks if a job_id exists in DB
        dry_run: If True, only report orphans without deleting

    Returns:
        List of orphaned directory names (deleted if not dry_run)
    """
    orphans: list[str] = []

    if not data_dir.exists():
        return orphans

    for entry in data_dir.iterdir():
        if not entry.is_dir():
            continue

        # Skip non-UUID directories (e.g., "downloads" subfolder)
        name = entry.name
        if len(name) != 36 or name.count("-") != 4:
            continue

        if not job_exists_func(name):
            orphans.append(name)
            if not dry_run:
                try:
                    shutil.rmtree(entry)
                    logger.info("Deleted orphaned directory: %s", name)
                except OSError as e:
                    logger.warning("Failed to delete orphan %s: %s", name, e)

    if orphans:
        action = "Found" if dry_run else "Cleaned"
        logger.info("%s %d orphaned directories", action, len(orphans))

    return orphans
