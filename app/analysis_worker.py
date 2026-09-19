#!/usr/bin/env python3
#
# app/analysis_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import multiprocessing
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from time import monotonic
from typing import Any, Final

from .audio_analysis import AudioAnalysisResult, extract_analysis
from .audio_cache import get_cached, store_cache
from .audio_hash import compute_audio_hash
from .db import get_settings, now_iso, update_job_if_status, with_finished_at
from .governor import governor

logger = logging.getLogger(__name__)


class SubmitResult(StrEnum):
    """Why :func:`submit_analysis` did or did not queue a job."""

    QUEUED = "queued"
    REJECTED_SHUTDOWN = "rejected_shutdown"
    QUEUE_FULL = "queue_full"


class AnalysisInterruptedError(Exception):
    """Raised when a shutdown aborts an analysis before it produced a result.

    The job row is deliberately left in ``analysis`` so the backlog fill at the
    next startup (app/main.py::_fill_analysis_queue) picks it up again.
    """


class JobStatus(StrEnum):
    """Canonical job status strings shared across DB writes and SSE events."""

    QUEUED = "queued"
    ANALYSIS = "analysis"
    ANALYSIS_DONE = "analysis_done"
    DONE = "done"
    ERROR = "error"


# Sentinel placed on the queue to wake workers during shutdown.
_SHUTDOWN_SENTINEL: object = object()

# Maximum number of pending analysis jobs kept in memory.
_QUEUE_MAXSIZE = 50
_DEFAULT_MAX_ANALYSIS_MINUTES = 15
_DEFAULT_ANALYSIS_TIMEOUT_MINUTES = 5
# The parent waits for the analysis child in short slices instead of one long
# join() so a shutdown is noticed within this interval rather than after the
# configured analysis timeout (up to an hour).
_JOIN_SLICE_SECONDS: Final[float] = 0.25
_analysis_queue: queue.Queue[AnalysisJob] | None = None
_queue_lock = threading.Lock()
_queued_job_ids: set[str] = set()
_queued_job_ids_lock = threading.Lock()
_worker_lock = threading.Lock()
_shutdown_event = threading.Event()
_workers_started = False
_worker_threads: list[threading.Thread] = []
# Live analysis children, so stop_analysis_workers() can end them instead of
# waiting out a join the worker threads are still blocked on. The threads are
# deliberately non-daemonic, so an unterminated child would keep the
# interpreter alive long after "Shutdown complete".
_active_analysis_processes: set[BaseProcess] = set()
_active_analysis_processes_lock = threading.Lock()
_status_callback: Callable[[dict[str, Any]], None] | None = None
_status_callback_lock = threading.Lock()


@dataclass(slots=True)
class AnalysisJob:
    job_id: str
    file_path: Path
    duration_seconds: float | None = None


def _analysis_runtime_limits() -> tuple[int | None, int]:
    """Return persisted BPM analysis duration/timeout limits."""
    try:
        settings = get_settings()
    except Exception:
        logger.warning("Could not read audio-analysis settings; falling back to defaults", exc_info=True)
        return _DEFAULT_MAX_ANALYSIS_MINUTES * 60, _DEFAULT_ANALYSIS_TIMEOUT_MINUTES * 60

    max_minutes = max(0, int(settings.get("audio_analysis_max_minutes", _DEFAULT_MAX_ANALYSIS_MINUTES)))
    timeout_minutes = max(1, int(settings.get("audio_analysis_timeout_minutes", _DEFAULT_ANALYSIS_TIMEOUT_MINUTES)))
    max_duration_seconds = None if max_minutes == 0 else max_minutes * 60
    return max_duration_seconds, timeout_minutes * 60


def _emit(job_id: str, status: str, message: str = "", **extra: Any) -> None:
    with _status_callback_lock:
        callback = _status_callback
    if callback is None:
        return
    payload = {
        "id": job_id,
        "status": status,
        "message": message,
        "timestamp": now_iso(),
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
    # with_finished_at() adds finished_at when status is terminal (see
    # app/db.py::TERMINAL_JOB_STATUSES); shared with worker.py so both modules
    # enforce the "terminal status => finished_at is set" invariant the same way.
    extra = with_finished_at(status, extra)
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


def submit_analysis(
    job_id: str,
    file_path: Path,
    *,
    duration_seconds: float | None = None,
    block: bool = False,
    timeout: float = 2.0,
) -> SubmitResult:
    """Queue a downloaded audio file for background analysis (see SubmitResult)."""
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
    audio_hash: str,
    bpm: int | None,
    bpm_confidence: float | None,
) -> bool:
    """Persist analysis results to the database and emit an SSE event.

    Returns False when the job left the analysis state in the meantime.
    """
    return _finalize_job(
        job_id,
        JobStatus.ANALYSIS_DONE,
        message,
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


def _stop_process(process: BaseProcess) -> None:
    """End an analysis child, escalating to SIGKILL if it ignores SIGTERM."""
    if not process.is_alive():
        return
    process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join()


def _await_process(process: BaseProcess, timeout_seconds: float) -> None:
    """Wait for an analysis child, staying responsive to shutdown.

    Raises TimeoutError past *timeout_seconds* and AnalysisInterruptedError when a
    shutdown arrives first; the child is ended in both cases so it can never
    outlive the worker thread that started it.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        if _shutdown_event.is_set():
            _stop_process(process)
            raise AnalysisInterruptedError("Audio analysis aborted by shutdown")

        remaining = deadline - monotonic()
        if remaining <= 0:
            _stop_process(process)
            raise TimeoutError(f"Audio analysis exceeded time limit ({timeout_seconds}s)")

        process.join(min(_JOIN_SLICE_SECONDS, remaining))
        if not process.is_alive():
            return


def _extract_analysis_with_timeout(path: Path) -> AudioAnalysisResult:
    """Run audio analysis with a hard wall-clock timeout.

    The analysis runs in a separate process so an overrun can be terminated
    without leaking a running thread or releasing the governor too early.
    """
    _, timeout_seconds = _analysis_runtime_limits()
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(target=_analysis_child, args=(path, child_connection))
    try:
        process.start()
        child_connection.close()
        with _active_analysis_processes_lock:
            _active_analysis_processes.add(process)
        _await_process(process, timeout_seconds)

        if not parent_connection.poll():
            if _shutdown_event.is_set():
                # stop_analysis_workers() ended the child from the outside; that
                # is a clean interruption, not an analysis failure.
                raise AnalysisInterruptedError("Audio analysis child was stopped during shutdown")
            raise RuntimeError("Audio analysis process exited without a result")

        succeeded, payload = parent_connection.recv()
        if not succeeded:
            raise RuntimeError(f"Audio analysis failed: {payload}")
        if not isinstance(payload, AudioAnalysisResult):
            raise RuntimeError("Audio analysis process returned an invalid result")
        return payload
    finally:
        with _active_analysis_processes_lock:
            _active_analysis_processes.discard(process)
        child_connection.close()
        parent_connection.close()
        _stop_process(process)


def _apply_analysis(job: AnalysisJob) -> None:
    """Hash the file, use the cache or run BPM extraction, then persist."""
    hash_value = compute_audio_hash(job.file_path)
    cached = get_cached(hash_value)

    if cached is not None:
        bpm = cached["bpm"]
        message = f"BPM {bpm}" if bpm is not None else "Audio analysis cached"
        if not _commit_analysis(
            job_id=job.job_id,
            message=message,
            audio_hash=hash_value,
            bpm=bpm,
            bpm_confidence=cached["bpm_confidence"],
        ):
            return
        logger.info("Audio analysis cache hit for %s: bpm=%s", job.job_id, bpm)
        return

    with governor.analysis_semaphore_sync:
        result = _extract_analysis_with_timeout(job.file_path)

    # Keep the hash -> analysis mapping regardless of this job's row: cache
    # first, then the state-guarded commit. A run that found no usable tempo is
    # cached too (audio_cache stores bpm=None) - re-running the full detector
    # cascade on every later download of the same audio would cost minutes of
    # CPU to reach the same "no BPM" answer.
    if not store_cache(
        hash_value,
        bpm=result.bpm,
        bpm_confidence=result.confidence,
    ):
        logger.debug("Cache write skipped/failed for %s", job.job_id)

    message = f"BPM {result.bpm or '-'}"

    if not _commit_analysis(
        job_id=job.job_id,
        message=message,
        audio_hash=hash_value,
        bpm=result.bpm,
        bpm_confidence=result.confidence,
    ):
        return

    logger.info(
        "Audio analysis complete for %s: bpm=%s, confidence=%s",
        job.job_id,
        result.bpm,
        f"{result.confidence:.2f}" if result.confidence is not None else "-",
    )


def _handle_analysis_job(job: AnalysisJob) -> None:
    """Skip over-long tracks and vanished files, else run _apply_analysis()."""
    max_duration_seconds, _ = _analysis_runtime_limits()
    if (
        max_duration_seconds is not None
        and job.duration_seconds is not None
        and job.duration_seconds > max_duration_seconds
    ):
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
            except AnalysisInterruptedError:
                # Leave the row in "analysis": the next startup re-queues it
                # (app/main.py::_fill_analysis_queue). Marking it as an error
                # would make a clean restart look like a failed analysis.
                logger.info("Audio analysis for %s interrupted by shutdown", job.job_id)
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
    """Start background audio analysis workers (default: the analysis limit)."""
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
    """Signal all workers to stop and wait for them to drain.

    Each gets a :data:`_SHUTDOWN_SENTINEL` so it leaves ``queue.get`` at once.
    """
    global _workers_started
    _shutdown_event.set()
    with _worker_lock:
        threads_to_join = list(_worker_threads)

    # End in-flight analysis children first. Without this a worker thread stays
    # blocked until the configured analysis timeout (up to an hour) expires, and
    # because these threads are non-daemonic the process would not exit either.
    with _active_analysis_processes_lock:
        running_processes = list(_active_analysis_processes)
    for process in running_processes:
        logger.info("Terminating in-flight audio analysis process %s", process.pid)
        _stop_process(process)

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
