#!/usr/bin/env python3
#
# app/utils/housekeeping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cleanup of job artifacts on the filesystem."""

import logging
import re
import secrets
import shutil
from collections.abc import Callable
from pathlib import Path
from time import time

from ..db import delete_expired_job_if_eligible, get_db
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
_RETENTION_QUARANTINE = ".retention-trash"
_STAGED_NAME_RE = re.compile(r"([0-9a-f-]{36})\.([0-9a-f]{16})")


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
            # lstat: age the link itself, so a dangling link still expires.
            if current_time - path.lstat().st_mtime <= _CACHE_MAX_AGE_SECONDS:
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


def _stage_expired_directory(job_id: str, jobs_root: Path) -> tuple[bool, Path | None]:
    """Atomically move a job directory aside while retention owns the DB writer lock."""
    if not _is_job_uuid(job_id):
        return False, None
    try:
        job_dir = resolve_within_root(Path(job_id), jobs_root, allow_symlink=False)
        if not job_dir.exists():
            return True, None
        if not job_dir.is_dir():
            return False, None
        quarantine = jobs_root / _RETENTION_QUARANTINE
        quarantine.mkdir(exist_ok=True)
        if quarantine.is_symlink() or not quarantine.is_dir():
            return False, None
        staged = quarantine / f"{job_id}.{secrets.token_hex(8)}"
        job_dir.rename(staged)
        return True, staged
    except (OSError, ValueError) as exc:
        logger.warning("Failed to stage expired job %s: %s", job_id, exc)
        return False, None


def _delete_staged_directory(staged: Path) -> None:
    try:
        shutil.rmtree(staged)
    except OSError as exc:
        logger.warning("Failed to remove staged job directory %s: %s", staged, exc)


def _recover_retention_quarantine(jobs_root: Path) -> None:
    """Restore uncommitted renames and finish deletions committed before a crash."""
    quarantine = jobs_root / _RETENTION_QUARANTINE
    if not quarantine.exists() or quarantine.is_symlink() or not quarantine.is_dir():
        return
    try:
        entries = tuple(quarantine.iterdir())
    except OSError as exc:
        logger.warning("Failed to scan retention quarantine %s: %s", quarantine, exc)
        return

    for staged in entries:
        match = _STAGED_NAME_RE.fullmatch(staged.name)
        if match is None or not _is_job_uuid(match.group(1)) or staged.is_symlink() or not staged.is_dir():
            continue
        job_id = match.group(1)
        original = jobs_root / job_id
        try:
            with get_db() as con:
                con.execute("BEGIN IMMEDIATE")
                exists = con.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None
                if exists and not original.exists():
                    staged.rename(original)
                    con.rollback()
                    continue
                con.rollback()
            if not exists:
                _delete_staged_directory(staged)
            else:
                logger.warning("Keeping staged job %s because its original directory exists", job_id)
        except OSError as exc:
            logger.warning("Failed to recover staged job %s: %s", job_id, exc)


def cleanup_expired_jobs(
    keep_days: int,
    jobs_root: Path,
    expired_job_ids_func: ExpiredJobIdsFunc,
) -> list[str]:
    """Delete files and database rows for expired jobs; return deleted IDs.

    The selector supplies candidates only. Each candidate is rechecked in an
    immediate SQLite transaction, which remains open through an atomic directory
    rename and matching row/link deletion. A retry that committed first is
    therefore skipped; a retry that arrives later cannot succeed after
    retention has claimed that job. Recursive deletion happens after commit;
    a later sweep recovers staged directories left by a crash or failed removal.
    Raises TypeError/ValueError for invalid days.
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int):
        raise TypeError("keep_days must be a non-negative integer")
    if keep_days < 0:
        raise ValueError("keep_days must be non-negative")

    try:
        jobs_root_resolved = jobs_root.resolve()
    except OSError as exc:
        logger.warning("Failed to resolve jobs directory %s: %s", jobs_root, exc)
        return []

    _recover_retention_quarantine(jobs_root_resolved)
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

    cleaned: list[str] = []
    for job_id in valid_ids:
        staged: Path | None = None

        def stage_directory(job_id: str = job_id) -> bool:
            nonlocal staged
            success, staged = _stage_expired_directory(job_id, jobs_root_resolved)
            return success

        try:
            deleted, links = delete_expired_job_if_eligible(job_id, keep_days, stage_directory)
        except Exception:
            if staged is not None:
                _recover_retention_quarantine(jobs_root_resolved)
            raise
        if staged is not None:
            _delete_staged_directory(staged)
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
