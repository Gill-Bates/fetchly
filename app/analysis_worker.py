#!/usr/bin/env python3
#
# app/analysis_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .audio_analysis import extract_analysis
from .audio_cache import get_cached, store_cache
from .audio_hash import compute_audio_hash
from .db import update_job
from .governor import governor

logger = logging.getLogger(__name__)


class SubmitResult(StrEnum):
    """Return value of :func:`submit_analysis` — tells the caller why a job
    was or was not queued so it can log/report the right message."""

    QUEUED = "queued"
    """Job accepted and placed in the analysis queue."""
    REJECTED_SHUTDOWN = "rejected_shutdown"
    """Worker is shutting down; job was not queued."""
    QUEUE_FULL = "queue_full"
    """Queue capacity exhausted; job was dropped."""


class JobStatus(StrEnum):
    """Canonical job status strings shared across DB writes and WS events."""

    QUEUED = "queued"
    ANALYSIS = "analysis"
    ANALYSIS_DONE = "analysis_done"
    DONE = "done"
    ERROR = "error"


# Sentinel placed on the queue to wake workers during shutdown.
_SHUTDOWN_SENTINEL: object = object()

# Maximum number of pending analysis jobs kept in memory.
_QUEUE_MAXSIZE = 50
# Tracks longer than 15 minutes are skipped to bound worst-case analysis time.
_MAX_ANALYSIS_DURATION_SECONDS = 900
_analysis_queue: queue.Queue[AnalysisJob] | None = None
_queue_lock = threading.Lock()
_worker_lock = threading.Lock()
_shutdown_event = threading.Event()
_workers_started = False
_worker_threads: list[threading.Thread] = []
_status_callback: Callable[[dict[str, Any]], None] | None = None
_status_callback_lock = threading.Lock()


@dataclass(slots=True)
class AnalysisJob:
    job_id: str
    file_path: Path
    duration_seconds: int | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _emit(job_id: str, status: str, message: str = "", **extra: Any) -> None:
    with _status_callback_lock:
        callback = _status_callback
    if callback is None:
        return
    payload = {
        "id": job_id,
        "status": status,
        "message": message,
        "timestamp": _now_iso(),
    }
    payload.update(extra)
    try:
        callback(payload)
    except Exception as exc:
        logger.warning("Status callback failed for %s: %s", job_id, exc)


def set_status_callback(callback: Callable[[dict[str, Any]], None] | None) -> None:
    global _status_callback
    with _status_callback_lock:
        _status_callback = callback


def get_analysis_queue() -> queue.Queue[AnalysisJob]:
    global _analysis_queue
    with _queue_lock:
        if _analysis_queue is None:
            _analysis_queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        return _analysis_queue


def _bpm_suffix(stem: str) -> str:
    return re.sub(r"(\s\[\d+ BPM\])+$", "", stem).strip()


def _rename_with_bpm(path: Path, bpm: int | None) -> Path:
    """Return a new path with the BPM value embedded in the filename.

    Always returns a path that either already existed or was just created by
    this call.  On any error the original *path* is returned unchanged.
    """
    if bpm is None or path.suffix.lower() != ".mp3":
        return path

    stem = _bpm_suffix(path.stem)
    new_path = path.with_name(f"{stem} [{bpm} BPM]{path.suffix}")
    if new_path == path:
        return path
    try:
        path.rename(new_path)
        logger.debug("Renamed %s -> %s", path.name, new_path.name)
        return new_path
    except FileExistsError:
        if new_path.is_file():
            logger.debug("BPM target already exists for %s, keeping %s", path.name, new_path.name)
            return new_path
        # Target exists but is not a regular file — leave the original in place.
        logger.warning("Cannot rename %s: target %s exists but is not a file", path, new_path)
        return path
    except OSError as exc:
        logger.warning("Could not rename %s with BPM suffix: %s", path.name, exc)
        return path


def submit_analysis(
    job_id: str,
    file_path: Path,
    *,
    duration_seconds: int | None = None,
    block: bool = False,
    timeout: float = 2.0,
) -> SubmitResult:
    """Queue a downloaded audio file for background analysis.

    Returns:
        :attr:`SubmitResult.QUEUED` when the job was accepted.
        :attr:`SubmitResult.REJECTED_SHUTDOWN` when the worker is stopping.
        :attr:`SubmitResult.QUEUE_FULL` when the queue has no free slot.
    """
    if _shutdown_event.is_set():
        logger.info("Skipping analysis enqueue for %s during shutdown", job_id)
        return SubmitResult.REJECTED_SHUTDOWN

    job = AnalysisJob(job_id=job_id, file_path=file_path, duration_seconds=duration_seconds)
    try:
        if block:
            get_analysis_queue().put(job, timeout=timeout)
        else:
            get_analysis_queue().put_nowait(job)
    except queue.Full:
        logger.warning("Audio analysis queue is full, skipping analysis for %s", job_id)
        return SubmitResult.QUEUE_FULL
    return SubmitResult.QUEUED


def _commit_analysis(
    *,
    job_id: str,
    message: str,
    final_path: Path,
    audio_hash: str,
    bpm: int | None,
    bpm_confidence: float | None,
) -> None:
    """Persist analysis results to the database and emit a WebSocket event."""
    update_job(
        job_id,
        status=JobStatus.ANALYSIS_DONE,
        message=message,
        filename=str(final_path),
        audio_hash=audio_hash,
        bpm=bpm,
        bpm_confidence=bpm_confidence,
    )
    _emit(
        job_id,
        JobStatus.ANALYSIS_DONE,
        message,
        filename=str(final_path),
        audio_hash=audio_hash,
        bpm=bpm,
        bpm_confidence=bpm_confidence,
    )


def _apply_analysis(job: AnalysisJob) -> None:
    """Hash the audio file, check the cache, run BPM extraction if needed,
    then persist results and emit a status event.
    """
    hash_value = compute_audio_hash(job.file_path)
    cached = get_cached(hash_value)

    if cached is not None:
        bpm = cached["bpm"]
        final_path = _rename_with_bpm(job.file_path, bpm)
        message = f"BPM {bpm}" if bpm is not None else "Audio analysis cached"
        _commit_analysis(
            job_id=job.job_id,
            message=message,
            final_path=final_path,
            audio_hash=hash_value,
            bpm=bpm,
            bpm_confidence=cached["bpm_confidence"],
        )
        logger.info("Audio analysis cache hit for %s: bpm=%s", job.job_id, bpm)
        return

    with governor.transcode_semaphore_sync:
        result = extract_analysis(job.file_path)

    final_path = _rename_with_bpm(job.file_path, result.bpm)
    message = f"BPM {result.bpm or '-'}"

    _commit_analysis(
        job_id=job.job_id,
        message=message,
        final_path=final_path,
        audio_hash=hash_value,
        bpm=result.bpm,
        bpm_confidence=result.confidence,
    )
    if result.bpm is not None:
        if not store_cache(
            hash_value,
            bpm=result.bpm,
            bpm_confidence=result.confidence,
        ):
            logger.debug("Cache write skipped/failed for %s", job.job_id)
    logger.info(
        "Audio analysis complete for %s: bpm=%s, confidence=%s",
        job.job_id,
        result.bpm,
        f"{result.confidence:.2f}" if result.confidence is not None else "-",
    )


def _handle_analysis_job(job: AnalysisJob) -> None:
    """Gate-check a job before passing it to :func:`_apply_analysis`.

    Skips analysis for tracks that exceed the duration limit or whose file
    has disappeared since the job was enqueued.
    """
    if job.duration_seconds is not None and job.duration_seconds > _MAX_ANALYSIS_DURATION_SECONDS:
        update_job(
            job.job_id,
            status=JobStatus.DONE,
            message="Finished (audio analysis skipped for long track)",
        )
        _emit(job.job_id, JobStatus.DONE, "Finished (audio analysis skipped for long track)")
        return

    if not job.file_path.is_file():
        update_job(job.job_id, status=JobStatus.DONE, message="Finished (audio file missing for analysis)")
        _emit(job.job_id, JobStatus.DONE, "Finished (audio file missing for analysis)")
        return

    _emit(job.job_id, JobStatus.ANALYSIS, "Analyzing audio...")
    _apply_analysis(job)


def _worker_loop() -> None:
    """Main loop executed by each analysis worker thread.

    Exits cleanly when a :data:`_SHUTDOWN_SENTINEL` is dequeued or when
    ``_shutdown_event`` is set and the queue is drained.
    """
    queue_obj = get_analysis_queue()
    current_thread = threading.current_thread()
    try:
        while not _shutdown_event.is_set():
            try:
                item = queue_obj.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is _SHUTDOWN_SENTINEL:
                queue_obj.task_done()
                break

            job: AnalysisJob = item  # type: ignore[assignment]
            try:
                _handle_analysis_job(job)
            except Exception as exc:
                logger.warning("Audio analysis failed for %s: %s", job.job_id, exc)
                update_job(job.job_id, status=JobStatus.DONE, message="Finished (audio analysis unavailable)")
                _emit(job.job_id, JobStatus.DONE, "Finished (audio analysis unavailable)")
            finally:
                queue_obj.task_done()
    finally:
        with _worker_lock:
            if current_thread in _worker_threads:
                _worker_threads.remove(current_thread)


def start_analysis_workers(n: int | None = None) -> None:
    global _workers_started
    if n is None:
        n = max(1, min(2, governor.transcode_limit))

    with _worker_lock:
        if _workers_started:
            return
        _shutdown_event.clear()
        get_analysis_queue()
        for _ in range(n):
            thread = threading.Thread(target=_worker_loop, daemon=False, name=f"analysis-worker-{_}")
            thread.start()
            _worker_threads.append(thread)
        _workers_started = True
        logger.info("Started %d audio analysis worker threads", n)


def stop_analysis_workers(timeout: float = 30.0) -> None:
    """Signal all workers to stop and wait for them to drain cleanly.

    Each worker receives a :data:`_SHUTDOWN_SENTINEL` so it exits the
    blocking ``queue.get`` immediately rather than waiting up to 0.5 s per
    iteration.
    """
    global _workers_started
    _shutdown_event.set()
    with _worker_lock:
        threads_to_join = list(_worker_threads)

    # Wake every worker exactly once so it can exit the get() call.
    q = get_analysis_queue()
    for _ in threads_to_join:
        try:
            q.put_nowait(_SHUTDOWN_SENTINEL)
        except queue.Full:
            break

    per_thread_timeout = max(1.0, timeout / max(len(threads_to_join), 1))
    alive_threads: list[threading.Thread] = []
    for thread in threads_to_join:
        thread.join(timeout=per_thread_timeout)
        if thread.is_alive():
            alive_threads.append(thread)
            logger.warning("Analysis worker %s did not stop cleanly", thread.name)

    with _worker_lock:
        _worker_threads[:] = alive_threads
        _workers_started = bool(alive_threads)