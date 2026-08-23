#!/usr/bin/env python3
#
# app/analysis_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from .audio_analysis import AudioAnalysisResult, extract_analysis
from .audio_cache import get_cached, store_cache
from .audio_hash import compute_audio_hash
from .db import update_job_if_status
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
_MAX_ANALYSIS_DURATION_SECONDS = max(0, int(os.environ.get("TUBEYOU_MAX_ANALYSIS_SECONDS", "900")))
_ANALYSIS_TIMEOUT_SECONDS = max(1, int(os.environ.get("TUBEYOU_AUDIO_ANALYSIS_TIMEOUT_SECONDS", "300")))
_analysis_queue: queue.Queue[AnalysisJob] | None = None
_queue_lock = threading.Lock()
_queued_job_ids: set[str] = set()
_queued_job_ids_lock = threading.Lock()
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
    """Set the status callback.

    The callback must be thread-safe, non-blocking, and safe to invoke from
    background worker threads.
    """
    global _status_callback
    with _status_callback_lock:
        _status_callback = callback


def _finalize_job(job_id: str, status: JobStatus, message: str, **extra: Any) -> bool:
    """Finalize an analysis job only while it still owns the analysis state."""
    if status in {JobStatus.DONE, JobStatus.ERROR, JobStatus.ANALYSIS_DONE}:
        extra.setdefault("finished_at", _now_iso())
    updated = update_job_if_status(
        job_id,
        (JobStatus.ANALYSIS,),
        status=status,
        message=message,
        **extra,
    )
    if updated:
        _emit(job_id, status, message, **extra)
        return True

    logger.info("Skipping analysis finalization for %s because its state changed", job_id)
    return False


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
        os.link(path, new_path)
    except FileExistsError:
        logger.warning("BPM target already exists, keeping original file: %s", new_path)
        return path
    except OSError as exc:
        logger.warning("Could not create BPM target for %s: %s", path.name, exc)
        return path

    try:
        path.unlink()
        logger.debug("Renamed %s -> %s", path.name, new_path.name)
        return new_path
    except OSError as exc:
        logger.warning("Could not remove original BPM source %s: %s", path.name, exc)
        return new_path


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

    with _queued_job_ids_lock:
        if job_id in _queued_job_ids:
            return SubmitResult.QUEUED

        job = AnalysisJob(job_id=job_id, file_path=file_path, duration_seconds=duration_seconds)
        try:
            if block:
                get_analysis_queue().put(job, timeout=timeout)
            else:
                get_analysis_queue().put_nowait(job)
        except queue.Full:
            logger.info("Audio analysis queue is full; deferring analysis for %s", job_id)
            return SubmitResult.QUEUE_FULL
        _queued_job_ids.add(job_id)
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
    _finalize_job(
        job_id,
        JobStatus.ANALYSIS_DONE,
        message,
        filename=str(final_path),
        audio_hash=audio_hash,
        bpm=bpm,
        bpm_confidence=bpm_confidence,
    )


def _analysis_child(path: Path, result_connection: Connection) -> None:
    """Run analysis in an isolated process and return its small result payload."""
    try:
        result_connection.send((True, extract_analysis(path)))
    except BaseException as exc:
        result_connection.send((False, repr(exc)))
    finally:
        result_connection.close()


def _extract_analysis_with_timeout(path: Path) -> AudioAnalysisResult:
    """Run audio analysis with a hard wall-clock timeout.

    The analysis runs in a separate process so an overrun can be terminated
    without leaking a running thread or releasing the governor too early.
    """
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(target=_analysis_child, args=(path, child_connection))
    try:
        process.start()
        child_connection.close()
        process.join(_ANALYSIS_TIMEOUT_SECONDS)

        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
            raise TimeoutError(f"Audio analysis exceeded time limit ({_ANALYSIS_TIMEOUT_SECONDS}s)")

        if not parent_connection.poll():
            raise RuntimeError("Audio analysis process exited without a result")

        succeeded, payload = parent_connection.recv()
        if not succeeded:
            raise RuntimeError(f"Audio analysis failed: {payload}")
        if not isinstance(payload, AudioAnalysisResult):
            raise RuntimeError("Audio analysis process returned an invalid result")
        return payload
    finally:
        child_connection.close()
        parent_connection.close()
        if process.is_alive():
            process.terminate()
            process.join()


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

    with governor.analysis_semaphore_sync:
        result = _extract_analysis_with_timeout(job.file_path)

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
        _finalize_job(
            job.job_id,
            JobStatus.DONE,
            "Finished (audio analysis skipped for long track)",
        )
        return

    if not job.file_path.is_file():
        _finalize_job(
            job.job_id,
            JobStatus.DONE,
            "Finished (audio file missing for analysis)",
        )
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

            if not isinstance(item, AnalysisJob):
                logger.error("Invalid item type in analysis queue: %s", type(item).__name__)
                queue_obj.task_done()
                continue

            job = item
            try:
                _handle_analysis_job(job)
            except BaseException as exc:
                if isinstance(exc, (SystemExit, KeyboardInterrupt, MemoryError)):
                    raise
                logger.exception("Audio analysis failed for %s", job.job_id)
                _finalize_job(
                    job.job_id,
                    JobStatus.ERROR,
                    "Audio analysis failed",
                )
            finally:
                with _queued_job_ids_lock:
                    _queued_job_ids.discard(job.job_id)
                queue_obj.task_done()
    finally:
        with _worker_lock:
            if current_thread in _worker_threads:
                _worker_threads.remove(current_thread)


def start_analysis_workers(n: int | None = None) -> None:
    """Start background audio analysis worker threads.

    Args:
        n: Number of workers. Defaults to the configured analysis limit.
    """
    global _workers_started
    if n is None:
        n = governor.analysis_limit

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
            q.put(_SHUTDOWN_SENTINEL, timeout=1.0)
        except queue.Full:
            logger.warning("Could not enqueue analysis shutdown sentinel, queue full")

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
