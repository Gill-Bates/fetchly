#!/usr/bin/env python3
#
# app/worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import contextlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple

from .analysis_worker import SubmitResult, submit_analysis
from .db import get_settings, now_iso, update_job, update_job_if_status, with_finished_at
from .governor import governor
from .utils.cookie_status import cookie_file_is_usable
from .utils.cookies import default_cookie_file
from .utils.duration import round_seconds
from .utils.fs import AUDIO_SOURCE_EXTENSIONS, get_data_dir
from .utils.platform import PLATFORM_COOKIE_FILENAMES, detect_platform
from .utils.watermark import VideoWatermark, build_watermark, video_filter_args
from .utils.youtube import normalize_info_url

logger = logging.getLogger(__name__)

type Job = tuple[str, str, str, str]
type StatusPayload = dict[str, Any]
type StatusCallback = Callable[[StatusPayload], None]

_BASE_DIR: Path | None = None
_base_dir_lock = threading.Lock()


def _get_base_dir() -> Path:
    """Lazily initialize the worker output directory."""
    global _BASE_DIR
    with _base_dir_lock:
        if _BASE_DIR is None:
            _BASE_DIR = get_data_dir()
            _BASE_DIR.mkdir(parents=True, exist_ok=True)
        return _BASE_DIR


def _resolve_cookie_file(platform: str) -> Path:
    filename = PLATFORM_COOKIE_FILENAMES.get(platform, "")
    if not filename:
        return Path()

    cookie_path = default_cookie_file(filename)
    try:
        if cookie_path.is_file() and cookie_path.stat().st_mode & 0o077:
            logger.warning("Cookie file %s is group/world accessible", cookie_path)
    except OSError:
        logger.warning("Could not inspect permissions for cookie file %s", cookie_path)
    return cookie_path


def _cookies_args_for_url(url: str) -> list[str]:
    """Return cookie arguments only when a *usable* cookie file exists.

    An all-expired jar is left out on purpose: yt-dlp loads cookies with
    ignore_expires=True and would send the dead session anyway, and platforms
    block a stale login harder than an anonymous request.
    """
    platform = detect_platform(url)
    if not platform:
        return []

    cookie_path = _resolve_cookie_file(platform)
    if not cookie_path.is_file():
        return []

    if not cookie_file_is_usable(cookie_path, platform):
        logger.warning(
            "Ignoring unusable cookie file for %s (%s) - downloading without cookies",
            platform,
            cookie_path,
        )
        return []

    return ["--cookies", str(cookie_path)]



# Lazily initialized; access via get_job_queue(). maxsize comes from the Governor.
_job_queue: queue.Queue[Job] | None = None
_queue_lock = threading.Lock()


def get_job_queue() -> queue.Queue[Job]:
    global _job_queue
    with _queue_lock:
        if _job_queue is None:
            maxsize = governor.queue_maxsize
            _job_queue = queue.Queue(maxsize=maxsize)
            logger.info("Job queue initialized with maxsize=%d (backpressure %s)",
                       maxsize, "enabled" if maxsize > 0 else "disabled")
        return _job_queue


# Ids of jobs that currently hold an in-memory queue slot. Persisted rows stay
# ``queued`` until a worker picks them up, so this set tells the backlog refill
# (app/main.py::_fill_download_queue) which rows must not be enqueued twice.
_queued_job_ids: set[str] = set()
_queued_ids_lock = threading.Lock()


def submit_download(job: Job) -> bool:
    """Give *job* an in-memory queue slot.

    Returns True when the job holds a slot (already queued counts as success)
    and False when the queue is full, leaving the persisted ``queued`` row for
    a later refill attempt.
    """
    job_id = job[0]
    with _queued_ids_lock:
        if job_id in _queued_job_ids:
            return True
        try:
            get_job_queue().put_nowait(job)
        except queue.Full:
            return False
        _queued_job_ids.add(job_id)
    return True


def _release_queue_slot(job_id: str) -> None:
    with _queued_ids_lock:
        _queued_job_ids.discard(job_id)


_status_callback: StatusCallback | None = None
_workers_started = False
_worker_lock = threading.Lock()
_shutdown_event = threading.Event()
_worker_threads: list[threading.Thread] = []
_cancel_lock = threading.Lock()
_cancelled_jobs: set[str] = set()
_active_lock = threading.Lock()
_active_processes: dict[str, subprocess.Popen[str]] = {}


_COMMAND_POLL_INTERVAL: Final = 1.0
# yt-dlp emits no machine-readable progress by default. --newline plus this
# template makes it print one parseable line per update on stdout; "NA" stands
# in for any value the extractor does not know.
_YTDLP_PROGRESS_MARKER: Final = "FETCHLY_DL"
_YTDLP_PROGRESS_FIELDS: Final = 6
_YTDLP_PROGRESS_TEMPLATE: Final = (
    f"download:{_YTDLP_PROGRESS_MARKER} "
    "%(progress.downloaded_bytes)s %(progress.total_bytes)s "
    "%(progress.total_bytes_estimate)s %(progress.eta)s "
    "%(progress.fragment_index)s %(progress.fragment_count)s"
)
# Bounds for the user-configurable --concurrent-fragments value. The API
# clamps to the same range; this guard also covers values written directly
# into the settings table.
_MIN_CONCURRENT_FRAGMENTS: Final = 1
_MAX_CONCURRENT_FRAGMENTS: Final = 16
# 0 means "Automatic": the governor sizes the value from the host's CPU quota
# and free memory (app/governor.py::recommended_concurrent_fragments).
_AUTO_CONCURRENT_FRAGMENTS: Final = 0
# Fallback for an unreadable setting or a failed host probe, not a UI default -
# the stored default is "Automatic" (see app/db.py).
_DEFAULT_CONCURRENT_FRAGMENTS: Final = 3
# Mirrors the "download_compatible_output" default in app/db.py.
_DEFAULT_COMPATIBLE_OUTPUT: Final = False
# Mirrors the "video_watermark" default in app/db.py.
_DEFAULT_VIDEO_WATERMARK: Final = True
# Watermark-only pass on "max" downloads (otherwise a pure download+remux).
# This encode is the only place a "max" download can lose quality, so the CRF -
# the quality lever, where the preset only trades speed for file size - scales
# with the source. A flat CRF 20 is transparent on a high-bitrate source but
# visibly softens a 240p/150 kbps upload, whose own compression artifacts get
# re-quantized on the way through. Smaller frames are cheap to encode, so they
# also get a slower preset; large ones stay fast to keep the pass affordable.
# (max height, preset, crf), first match wins.
_WATERMARK_X264_LADDER: Final[tuple[tuple[int, str, str], ...]] = (
    (576, "medium", "16"),
    (1080, "fast", "18"),
)
# The compatibility promise, in ffmpeg/ffprobe codec names: H.264 video and
# AAC audio in MP4 is the one combination that plays on Safari and iOS, on
# TVs and in editing software. yt-dlp is asked to pick such a rendition first
# (_COMPATIBLE_FORMAT_SORT below), so the re-encode only ever runs for a source
# that has none.
_COMPATIBLE_VIDEO_CODECS: Final[frozenset[str]] = frozenset({"h264"})
_COMPATIBLE_AUDIO_CODECS: Final[frozenset[str]] = frozenset({"aac"})
_COMPATIBLE_AAC_BITRATE: Final = "192k"
# vcodec before res: a 1080p H.264 rendition beats a 2160p AV1 one. Mirrors
# yt-dlp's own "mp4" preset alias minus its format filter, which would fight
# with the -f expression the caller already passes.
_COMPATIBLE_FORMAT_SORT: Final = "vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac"

# Beyond 1080p, and whenever the height cannot be read: those sources carry
# enough bitrate of their own that CRF 20 stays transparent, and a 4K frame is
# expensive enough that the cheapest preset is the only affordable one.
_WATERMARK_X264_FALLBACK: Final[tuple[str, str]] = ("veryfast", "20")
_DEFAULT_DOWNLOAD_TIMEOUT_MINUTES: Final = 60
_DEFAULT_TRANSCODE_TIMEOUT_MINUTES: Final = 120
_DEFAULT_DOWNLOAD_MAX_FILESIZE_GIB: Final = 4


_QUALITY_LABELS: Final[dict[str, str]] = {
    "max": "maxQuality",
    "medium": "mediumQuality",
    "small": "smallQuality",
    "best": "bestQuality",
}
_VIDEO_SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
})


class JobCancelledError(Exception):
    """Raised when a job is explicitly cancelled by user request."""


class ShutdownError(Exception):
    """Raised when a running command fails because the worker is shutting down."""


class DownloadRuntimeLimits(NamedTuple):
    download_timeout_seconds: int
    transcode_timeout_seconds: int
    max_filesize_arg: str


_LOGIN_REQUIRED_PATTERNS: Final[tuple[str, ...]] = (
    "login required",
    "login_required",
    "rate-limit reached",
    "not available, rate-limit",
    "cookies-from-browser or --cookies",
)


def _user_facing_error(url: str, exc: Exception) -> str:
    """Turn a worker exception into an actionable user-visible message."""
    raw = str(exc)
    raw_lower = raw.lower()

    if any(p in raw_lower for p in _LOGIN_REQUIRED_PATTERNS):
        platform = detect_platform(url)
        cookie_file = PLATFORM_COOKIE_FILENAMES.get(platform or "", "")
        cookie_path = _resolve_cookie_file(platform or "") if platform else Path()
        if platform and cookie_file and not cookie_file_is_usable(cookie_path, platform):
            action = "Refresh" if cookie_path.is_file() else "Add"
            return (
                f"{platform.capitalize()} requires authentication. "
                f"{action} the cookies for {platform.capitalize()} under "
                f"Settings → Integrations."
            )
        return f"Platform requires authentication: {raw[:200]}"

    return f"Job failed: {raw[:300]}"


def _download_runtime_limits() -> DownloadRuntimeLimits:
    """Return persisted download/transcode limits with documented fallbacks."""
    try:
        settings = get_settings()
    except Exception:
        logger.warning("Could not read runtime download limits; falling back to defaults", exc_info=True)
        return DownloadRuntimeLimits(
            download_timeout_seconds=_DEFAULT_DOWNLOAD_TIMEOUT_MINUTES * 60,
            transcode_timeout_seconds=_DEFAULT_TRANSCODE_TIMEOUT_MINUTES * 60,
            max_filesize_arg=f"{_DEFAULT_DOWNLOAD_MAX_FILESIZE_GIB}G",
        )

    download_timeout_minutes = max(1, int(settings.get("download_timeout_minutes", _DEFAULT_DOWNLOAD_TIMEOUT_MINUTES)))
    transcode_timeout_minutes = max(
        1,
        int(settings.get("transcode_timeout_minutes", _DEFAULT_TRANSCODE_TIMEOUT_MINUTES)),
    )
    max_filesize_gib = max(1, int(settings.get("download_max_filesize_gib", _DEFAULT_DOWNLOAD_MAX_FILESIZE_GIB)))
    return DownloadRuntimeLimits(
        download_timeout_seconds=download_timeout_minutes * 60,
        transcode_timeout_seconds=transcode_timeout_minutes * 60,
        max_filesize_arg=f"{max_filesize_gib}G",
    )


def set_status_callback(callback: StatusCallback | None) -> None:
    global _status_callback
    _status_callback = callback


def signal_shutdown() -> None:
    """Set the worker shutdown flag so threads stop accepting new work."""
    _shutdown_event.set()


def cancel_job(job_id: str) -> None:
    """Mark a job for cancellation."""
    # Best-effort: terminate currently running subprocess for immediate cancel.
    with _active_lock:
        proc = _active_processes.get(job_id)
    with _cancel_lock:
        _cancelled_jobs.add(job_id)
    if proc is not None:
        _terminate_process(proc, grace_seconds=1.0)


def clear_cancellation(job_id: str) -> None:
    """Drop a cancel marker before a job id is put back to work.

    A job cancelled while queued keeps its marker until a worker pops it off,
    so an in-place retry would otherwise be cancelled by that stale marker.
    """
    with _cancel_lock:
        _cancelled_jobs.discard(job_id)


def is_job_cancelled(job_id: str) -> bool:
    with _cancel_lock:
        return job_id in _cancelled_jobs


def _check_cancellation(job_id: str) -> None:
    if is_job_cancelled(job_id):
        raise JobCancelledError(f"Job {job_id} was cancelled")


def _check_shutdown() -> None:
    if _shutdown_event.is_set():
        raise ShutdownError("Shutdown requested")


# Live-progress fields that only ever go out over SSE; jobs has no columns
# for them, so update_job_if_status() must never see them.
_TRANSIENT_STATUS_FIELDS: Final = frozenset({"progress", "eta_seconds"})


def _emit(job_id: str, status: str, message: str = "", **extra: Any) -> None:
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
        logger.warning("Status callback failed for %s: %s", job_id, exc, exc_info=True)


def _transition(job_id: str, status: str, message: str = "", **extra: Any) -> None:
    """Persist a job state change and emit the matching status event.

    ``with_finished_at()`` adds ``finished_at`` when *status* is terminal (see
    app/db.py::TERMINAL_JOB_STATUSES), so call sites do not each have to pass
    it - the same helper analysis_worker.py uses, so both workers enforce the
    invariant identically.
    """
    extra = with_finished_at(status, extra)
    update_job(job_id, status=status, message=message, **extra)
    _emit(job_id, status, message, **extra)


def _transition_if_processing(job_id: str, status: str, message: str = "", **extra: Any) -> bool:
    """Persist a terminal update only if the job is still in a worker-owned state."""
    extra = with_finished_at(status, extra)
    updated = update_job_if_status(
        job_id,
        ("processing", "downloading", "transcoding"),
        status=status,
        message=message,
        **extra,
    )
    if updated:
        _emit(job_id, status, message, **extra)
        return True

    logger.info("Skipping %s transition for %s because the job state changed before writeback", status, job_id)
    return False


def _transition_worker_status(job_id: str, status: str, message: str = "", **extra: Any) -> bool:
    """Persist worker-owned in-flight statuses and emit matching status events.

    ``progress``/``eta_seconds`` are SSE-only: there is no column for them, so
    they must reach _emit() but not update_job_if_status().
    """
    persisted_extra = {k: v for k, v in extra.items() if k not in _TRANSIENT_STATUS_FIELDS}
    updated = update_job_if_status(
        job_id,
        ("processing", "downloading", "transcoding"),
        status=status,
        message=message,
        **persisted_extra,
    )
    if updated:
        _emit(job_id, status, message, **extra)
        return True
    return False


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal the child's process group.

    Every subprocess here is started with ``start_new_session=True``, which
    makes the child a process-group leader - so the PGID *is* the child PID and
    no os.getpgid() lookup is needed. Skipping it also closes the window where
    the child exits between the lookup and the signal: Popen has not reaped it
    yet, so its PID cannot have been reused.
    """
    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.debug("Permission denied signalling process group pgid=%s", proc.pid)


def _terminate_process(proc: subprocess.Popen[str], *, grace_seconds: float = 2.0) -> None:
    """Terminate a subprocess group and force-kill it after a grace period.

    All subprocesses in this module use ``start_new_session=True``. This makes
    the child process a process-group leader, so terminating the resolved PGID
    also stops descendants such as yt-dlp's ffmpeg process.
    """
    if proc.poll() is not None:
        return

    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    _signal_process_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.debug("Failed to fully stop subprocess pid=%s", proc.pid)


def _register_active_process(job_id: str, proc: subprocess.Popen[str]) -> None:
    """Register the only subprocess allowed to run for a job."""
    with _active_lock:
        existing = _active_processes.get(job_id)
        if existing is not None and existing.poll() is None:
            raise RuntimeError(f"Job {job_id} already has an active subprocess")
        _active_processes[job_id] = proc


def _unregister_active_process(job_id: str, proc: subprocess.Popen[str]) -> None:
    """Remove a subprocess registration without hiding a newer process."""
    with _active_lock:
        if _active_processes.get(job_id) is proc:
            _active_processes.pop(job_id, None)


def _run_cmd(
    cmd: list[str],
    *,
    timeout: int,
    capture_stdout: bool = False,
    job_id: str | None = None,
) -> str:
    logger.debug("Executing command: %s", " ".join(cmd))
    if job_id is not None:
        _check_cancellation(job_id)
    _check_shutdown()

    started = time.monotonic()
    stdout_value = ""
    stdout_collected = False
    proc: subprocess.Popen[str] | None = None
    stderr_tmp: tempfile._TemporaryFileWrapper[str] | None = None
    try:
        # Closed in the matching finally: the handle has to outlive the
        # `with subprocess.Popen(...)` block it is passed into.
        stderr_tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w+", encoding="utf-8", errors="replace", delete=True
        )
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            # Keep stderr in a temp file for diagnostics without PIPE deadlocks.
            stderr=stderr_tmp,
            start_new_session=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as process:
            proc = process
            try:
                if job_id is not None:
                    _register_active_process(job_id, proc)
                    # A cancel request can arrive between Popen and registration.
                    _check_cancellation(job_id)
                _check_shutdown()

                while proc.poll() is None:
                    if job_id is not None:
                        _check_cancellation(job_id)
                    _check_shutdown()

                    elapsed = time.monotonic() - started
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise RuntimeError(f"Command timed out after {timeout}s")

                    try:
                        poll_timeout = min(_COMMAND_POLL_INTERVAL, remaining)
                        if capture_stdout:
                            stdout_value, _ = proc.communicate(timeout=poll_timeout)
                            stdout_collected = True
                        else:
                            proc.wait(timeout=poll_timeout)
                    except subprocess.TimeoutExpired:
                        continue

                if capture_stdout and not stdout_collected:
                    # The child can exit in the race window between a
                    # TimeoutExpired and the next poll(), which ends the loop
                    # without any completed communicate(). Drain the pipe once
                    # more so callers (ffprobe) do not see an empty result -
                    # communicate() resumes from the partially read buffers.
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        stdout_value, _ = proc.communicate(timeout=_COMMAND_POLL_INTERVAL)

                if job_id is not None:
                    _check_cancellation(job_id)
                _check_shutdown()

                if proc.returncode != 0:
                    executable = cmd[0] if cmd else "command"
                    stderr_tail = _stderr_tail(stderr_tmp)
                    if stderr_tail:
                        logger.warning(
                            "%s failed with exit code %s. stderr tail: %s",
                            executable,
                            proc.returncode,
                            stderr_tail,
                        )
                        raise RuntimeError(f"{executable} failed with exit code {proc.returncode}: {stderr_tail}")

                    raise RuntimeError(f"{executable} failed with exit code {proc.returncode}")
            except Exception:
                if proc.poll() is None:
                    _terminate_process(proc)
                raise
    except (JobCancelledError, ShutdownError, RuntimeError):
        raise
    except Exception as exc:
        if _shutdown_event.is_set():
            raise ShutdownError("Shutdown requested") from exc
        logger.exception("Command failed: %s", " ".join(cmd))
        # Named, not generic: this is what the user sees, and "ffmpeg not
        # found" is a very different problem from "permission denied".
        raise RuntimeError(f"Command execution failed: {exc}") from exc
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if stderr_tmp is not None:
            try:
                stderr_tmp.close()
            except Exception:
                logger.debug("Could not close temporary stderr file", exc_info=True)
        if job_id is not None and proc is not None:
            _unregister_active_process(job_id, proc)

    return stdout_value if capture_stdout else ""


def _parse_ytdlp_number(raw: str) -> float | None:
    """Parse one field of a yt-dlp progress line ("NA" means unknown)."""
    value = raw.strip()
    if not value or value == "NA":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_ffmpeg_timecode(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":", 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)
    except (TypeError, ValueError):
        return None


def _ffmpeg_out_seconds(progress_state: dict[str, str]) -> float | None:
    timecode = str(progress_state.get("out_time") or "").strip()
    if timecode:
        parsed = _parse_ffmpeg_timecode(timecode)
        if parsed is not None:
            return parsed

    for key in ("out_time_us", "out_time_ms"):
        raw = str(progress_state.get(key) or "").strip()
        if not raw:
            continue
        try:
            return float(raw) / 1_000_000.0
        except ValueError:
            continue

    return None


def _redact_urls(text: str) -> str:
    """Strip query strings from URLs in text bound for the log or the UI.

    yt-dlp and ffmpeg quote the failing media URL, and on a CDN that URL
    carries signed access tokens. The path is what makes an error readable;
    the credentials in the query string are not.
    """
    return re.sub(r"(https?://[^\s?]+)\?\S*", r"\1?<redacted>", text)


def _stderr_tail(stderr_tmp: Any, *, limit: int = 800) -> str:
    """Return the tail of a subprocess' captured stderr for error messages."""
    if stderr_tmp is None:
        return ""
    try:
        stderr_tmp.seek(0)
        return _redact_urls(stderr_tmp.read()[-limit:].strip())
    except Exception:
        return ""


def _read_progress_lines(pipe: Any, target_queue: queue.Queue[str | None]) -> None:
    try:
        for raw_line in iter(pipe.readline, ""):
            try:
                target_queue.put(raw_line.rstrip(), timeout=0.1)
            except queue.Full:
                continue
    finally:
        with contextlib.suppress(queue.Full):
            target_queue.put(None, timeout=0.1)


def _emit_ffmpeg_progress(
    job_id: str,
    *,
    message: str,
    out_seconds: float,
    duration_seconds: float | None,
    started_at: float,
    last_progress: int,
) -> int:
    if duration_seconds is None or duration_seconds <= 0:
        return last_progress

    pct = max(0, min(100, round((out_seconds / duration_seconds) * 100)))
    if pct <= last_progress:
        return last_progress

    eta_seconds: int | None = None
    if 0 < pct < 100:
        elapsed = max(0.0, time.monotonic() - started_at)
        total_estimate = elapsed / (pct / 100.0)
        eta_seconds = max(0, round(total_estimate - elapsed))

    _emit(job_id, "transcoding", message, progress=pct, eta_seconds=eta_seconds)
    return pct


class _DownloadProgress:
    """Turn yt-dlp's per-file counters into one job-wide percentage.

    A video job usually pulls two files (video + audio, merged afterwards),
    each counting from zero, so the percentage is derived from aggregate bytes:
    a finished file's size is folded into a base offset when the next counter
    restarts. Sources that report no size (some fragmented HLS/DASH streams)
    fall back to the fragment count, and emit nothing when even that is missing.
    """

    __slots__ = ("_base_bytes", "_current_bytes", "_job_id", "_last_pct", "_message")

    def __init__(self, job_id: str, message: str) -> None:
        self._job_id = job_id
        self._message = message
        self._base_bytes = 0.0
        self._current_bytes = 0.0
        self._last_pct = -1

    def feed(self, line: str) -> None:
        if not line.startswith(_YTDLP_PROGRESS_MARKER):
            return

        fields = line[len(_YTDLP_PROGRESS_MARKER):].split()
        if len(fields) != _YTDLP_PROGRESS_FIELDS:
            return

        downloaded = _parse_ytdlp_number(fields[0])
        if downloaded is None:
            return

        total = _parse_ytdlp_number(fields[1]) or _parse_ytdlp_number(fields[2])
        eta = _parse_ytdlp_number(fields[3])
        fragment_index = _parse_ytdlp_number(fields[4])
        fragment_count = _parse_ytdlp_number(fields[5])

        if downloaded < self._current_bytes:
            # The counter restarted: the previous stream finished, so keep its
            # transferred bytes as the floor for everything that follows.
            self._base_bytes += self._current_bytes
        self._current_bytes = downloaded

        fraction = self._fraction(downloaded, total, fragment_index, fragment_count)
        if fraction is None:
            return

        # Capped below 100 while the process runs: a job that reads "100%" but
        # keeps working (merging, or a second stream still to come) is worse
        # than one that sits at 99% for a moment. finish() closes the gap.
        pct = min(99, max(0, int(fraction * 100)))
        if pct <= self._last_pct:
            return
        self._last_pct = pct

        _emit(
            self._job_id,
            "downloading",
            self._message,
            progress=pct,
            eta_seconds=int(eta) if eta is not None else None,
        )

    def _fraction(
        self,
        downloaded: float,
        total: float | None,
        fragment_index: float | None,
        fragment_count: float | None,
    ) -> float | None:
        if total is not None and total > 0:
            overall_total = self._base_bytes + total
            if overall_total > 0:
                return min(1.0, (self._base_bytes + downloaded) / overall_total)
        if fragment_count is not None and fragment_count > 0 and fragment_index is not None:
            return min(1.0, fragment_index / fragment_count)
        return None

    def finish(self) -> None:
        """Emit the closing 100% - only if a percentage was ever shown."""
        if self._last_pct < 0:
            return
        _emit(self._job_id, "downloading", self._message, progress=100, eta_seconds=0)


def _run_ytdlp_download(cmd: list[str], *, job_id: str, message: str) -> None:
    """Run a yt-dlp download command and stream its progress to the client."""
    tracker = _DownloadProgress(job_id, message)
    limits = _download_runtime_limits()
    _run_cmd_streaming(
        cmd,
        timeout=limits.download_timeout_seconds,
        job_id=job_id,
        on_line=tracker.feed,
    )
    tracker.finish()


def _handle_progress_line(job_id: str, line: str, on_line: Callable[[str], None]) -> None:
    """Feed one output line to a progress handler, absorbing handler failures.

    Progress reporting is cosmetic: a handler bug must never abort a download or
    transcode that is otherwise fine.
    """
    try:
        on_line(line)
    except Exception:
        logger.debug("Progress handler failed for job %s", job_id, exc_info=True)


def _run_cmd_streaming(
    cmd: list[str],
    *,
    timeout: int,
    job_id: str,
    on_line: Callable[[str], None],
) -> None:
    """Run *cmd*, feeding every stdout line to *on_line* while it still runs.

    Shared by the ffmpeg transcode and the yt-dlp download. A reader thread
    drains stdout so a full pipe cannot block the child; stderr goes to a temp
    file so its tail can be surfaced without a second pipe to deadlock on.
    """
    logger.debug("Executing command with progress: %s", " ".join(cmd))
    _check_cancellation(job_id)
    _check_shutdown()

    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    stderr_tmp: tempfile._TemporaryFileWrapper[str] | None = None
    progress_lines: queue.Queue[str | None] = queue.Queue(maxsize=500)
    reader_thread: threading.Thread | None = None

    try:
        # Closed in the matching finally: the handle has to outlive the
        # `with subprocess.Popen(...)` block it is passed into.
        stderr_tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w+", encoding="utf-8", errors="replace", delete=True
        )
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_tmp,
            start_new_session=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process:
            proc = process
            try:
                _register_active_process(job_id, proc)
                # A cancel request can arrive between Popen and registration.
                _check_cancellation(job_id)
                _check_shutdown()

                if proc.stdout is not None:
                    reader_thread = threading.Thread(
                        target=_read_progress_lines,
                        args=(proc.stdout, progress_lines),
                        daemon=True,
                    )
                    reader_thread.start()

                while proc.poll() is None:
                    _check_cancellation(job_id)
                    _check_shutdown()

                    elapsed = time.monotonic() - started
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise RuntimeError(f"Command timed out after {timeout}s")

                    try:
                        line = progress_lines.get(timeout=min(_COMMAND_POLL_INTERVAL, remaining))
                    except queue.Empty:
                        continue

                    if line is None:
                        continue

                    _handle_progress_line(job_id, line, on_line)

                # The child can exit with lines still queued: a command that
                # finishes inside one poll interval would otherwise have its
                # last - and possibly only - progress update dropped. Let the
                # reader finish off the closed pipe first, then drain; the queue
                # is bounded, so this cannot run away.
                if reader_thread is not None:
                    reader_thread.join(timeout=_COMMAND_POLL_INTERVAL)
                while True:
                    try:
                        pending = progress_lines.get_nowait()
                    except queue.Empty:
                        break
                    if pending is None:
                        break
                    _handle_progress_line(job_id, pending, on_line)

                _check_cancellation(job_id)
                _check_shutdown()

                if proc.returncode != 0:
                    executable = cmd[0] if cmd else "command"
                    stderr_tail = _stderr_tail(stderr_tmp)
                    if stderr_tail:
                        logger.warning(
                            "%s failed with exit code %s. stderr tail: %s",
                            executable, proc.returncode, stderr_tail,
                        )
                        raise RuntimeError(f"{executable} failed with exit code {proc.returncode}: {stderr_tail}")
                    raise RuntimeError(f"{executable} failed with exit code {proc.returncode}")
            except Exception:
                if proc.poll() is None:
                    _terminate_process(proc)
                raise
    except (JobCancelledError, ShutdownError, RuntimeError):
        raise
    except Exception as exc:
        if _shutdown_event.is_set():
            raise ShutdownError("Shutdown requested") from exc
        logger.exception("Command failed: %s", " ".join(cmd))
        # Named, not generic: this is what the user sees, and "ffmpeg not
        # found" is a very different problem from "permission denied".
        raise RuntimeError(f"Command execution failed: {exc}") from exc
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if reader_thread is not None:
            reader_thread.join(timeout=0.2)
        if stderr_tmp is not None:
            try:
                stderr_tmp.close()
            except Exception:
                logger.debug("Could not close temporary stderr file", exc_info=True)
        if proc is not None:
            _unregister_active_process(job_id, proc)


def _run_ffmpeg_transcode(
    cmd: list[str],
    *,
    timeout: int,
    job_id: str,
    message: str,
    duration_seconds: float | None,
) -> None:
    started = time.monotonic()
    progress_state: dict[str, str] = {}
    last_progress = -1

    def handle_line(line: str) -> None:
        nonlocal last_progress
        if "=" not in line:
            return

        key, value = line.split("=", 1)
        progress_state[key] = value
        if key != "progress":
            return

        out_seconds = _ffmpeg_out_seconds(progress_state)
        if out_seconds is not None:
            last_progress = _emit_ffmpeg_progress(
                job_id,
                message=message,
                out_seconds=out_seconds,
                duration_seconds=duration_seconds,
                started_at=started,
                last_progress=last_progress,
            )

        if value == "end":
            last_progress = _emit_ffmpeg_progress(
                job_id,
                message=message,
                out_seconds=float(duration_seconds or 0),
                duration_seconds=duration_seconds,
                started_at=started,
                last_progress=last_progress,
            )
        progress_state.clear()

    _run_cmd_streaming(cmd, timeout=timeout, job_id=job_id, on_line=handle_line)

    if duration_seconds and last_progress < 100:
        _emit(job_id, "transcoding", message, progress=100, eta_seconds=0)


_WIN_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

# Byte budget for the sanitized title. Linux caps a filename at 255 bytes;
# the rest is headroom for the suffixes the stem picks up on the way to disk.
_STEM_MAX_BYTES: Final[int] = 200


def sanitize_filename(name: str, max_len: int = 120, max_bytes: int = _STEM_MAX_BYTES) -> str:
    # Strip control characters (0x00-0x1F, 0x7F) and Windows-reserved chars
    cleaned = re.sub(r"[\x00-\x1f\x7f\\/:*?\"<>|\[\]]", "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.upper() in _WIN_RESERVED_NAMES:
        cleaned = "video"
    cleaned = cleaned[:max_len]
    # NAME_MAX is 255 *bytes* on Linux, so a 120-character CJK or Cyrillic
    # title can be three times over the limit while looking short. Everything
    # the callers append afterwards - " (maxQuality)", ".source",
    # ".finalized", the extension - has to fit in the remainder, or the job
    # fails with ENAMETOOLONG on a title the user cannot do anything about.
    while len(cleaned.encode("utf-8")) > max_bytes:
        cleaned = cleaned[:-1]
    return cleaned.rstrip(" .") or "video"


def _quality_label(quality: str) -> str:
    return _QUALITY_LABELS.get((quality or "").lower(), f"{quality}Quality" if quality else "defaultQuality")


def _build_output_stem(job_id: str, url: str, quality: str, media_type: str) -> str:
    try:
        title_raw = _run_cmd(
            [
                "yt-dlp",
                "--no-playlist",
                # Same fast-fail policy as the download itself (see
                # _build_ytdlp_cmd): a blocked source must not hold a worker
                # slot through yt-dlp's default ten-retry storm just to read a
                # title the job can do without.
                "--socket-timeout", "30",
                "--retries", "3",
                "--extractor-retries", "1",
                *_cookies_args_for_url(url),
                "--get-title",
                "--",
                url,
            ],
            timeout=120,
            capture_stdout=True,
            job_id=job_id,
        ).strip()
    except (JobCancelledError, ShutdownError):
        raise
    except Exception as exc:
        logger.warning("Could not fetch video title, using fallback filename: %s", exc)
        title_raw = "video"
    title = sanitize_filename(title_raw)
    if media_type == "audio":
        # Audio is always pulled losslessly, so the quality label would say the
        # same thing on every file. Only video has renditions worth naming.
        return title
    return f"{title} ({_quality_label(quality)})"


def _rename_thumbnail(job_dir: Path) -> None:
    for thumb in job_dir.glob("*.jpg"):
        if thumb.name != "thumbnail.jpg":
            thumb.rename(job_dir / "thumbnail.jpg")
            break


def _resolve_concurrent_fragments(value: object) -> int:
    """Turn the stored --concurrent-fragments setting into a usable count.

    A positive value is the user's choice, clamped to the settings API range.
    ``0`` or below selects "Automatic": resolved per download from the host's
    CPU quota and free memory.
    """
    try:
        fragments = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid concurrent-fragments setting %r; using %d", value, _DEFAULT_CONCURRENT_FRAGMENTS)
        return _DEFAULT_CONCURRENT_FRAGMENTS

    if fragments > _AUTO_CONCURRENT_FRAGMENTS:
        return min(max(fragments, _MIN_CONCURRENT_FRAGMENTS), _MAX_CONCURRENT_FRAGMENTS)

    try:
        automatic = governor.recommended_concurrent_fragments()
    except Exception:
        logger.warning(
            "Could not size concurrent fragments from the host; using %d",
            _DEFAULT_CONCURRENT_FRAGMENTS,
            exc_info=True,
        )
        return _DEFAULT_CONCURRENT_FRAGMENTS

    logger.debug("Automatic concurrent fragments: %d", automatic)
    return automatic


def _compatible_output_required(settings: Mapping[str, Any]) -> bool:
    """Whether the finished file must be H.264/AAC.

    The watermark implies it: that pass runs libx264 either way, so taking the
    compatible container along costs nothing. The user's own setting is never
    written back from here - it stays whatever they chose, and turning the
    watermark off restores it.
    """
    if bool(settings.get("video_watermark", _DEFAULT_VIDEO_WATERMARK)):
        return True
    return bool(settings.get("download_compatible_output", _DEFAULT_COMPATIBLE_OUTPUT))


def _download_tuning() -> tuple[int, bool]:
    """Return ``(concurrent_fragments, compatible_output)`` from the settings.

    A failing read falls back to defaults rather than aborting the download.
    """
    try:
        settings = get_settings()
    except Exception:
        logger.warning("Could not read download settings; falling back to defaults", exc_info=True)
        return _DEFAULT_CONCURRENT_FRAGMENTS, _DEFAULT_VIDEO_WATERMARK

    fragments = _resolve_concurrent_fragments(
        settings.get("download_concurrent_fragments", _AUTO_CONCURRENT_FRAGMENTS)
    )
    return fragments, _compatible_output_required(settings)


def _watermark_config() -> tuple[bool, str]:
    """Return ``(enabled, hostname)`` from settings.

    A failing read falls back to defaults rather than aborting the download.
    """
    try:
        settings = get_settings()
    except Exception:
        logger.warning("Could not read watermark settings; falling back to defaults", exc_info=True)
        return _DEFAULT_VIDEO_WATERMARK, ""

    enabled = bool(settings.get("video_watermark", _DEFAULT_VIDEO_WATERMARK))
    return enabled, str(settings.get("public_hostname", "") or "")


def _probe_video_size(path: Path, *, job_id: str | None = None) -> tuple[int, int] | None:
    """Pixel size of the first video stream, or ``None`` when it cannot be read."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        output = _run_cmd(cmd, timeout=20, capture_stdout=True, job_id=job_id)
        streams = json.loads(output or "{}").get("streams", [])
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (JobCancelledError, ShutdownError):
        raise
    except Exception as exc:
        logger.warning("Could not read video dimensions of %s: %s", path, exc)
        return None

    return (width, height) if width > 0 and height > 0 else None


def _scaled_size(source: tuple[int, int], target_height: int) -> tuple[int, int]:
    """Output size produced by ``scale=-2:'min(target_height,ih)'``."""
    width, height = source
    out_height = min(target_height, height)
    # -2 rounds the width to the nearest even number, which is what libx264
    # needs and what the badge has to be sized against.
    out_width = max(2, round(width * out_height / height / 2) * 2)
    return out_width, out_height


def _watermark_x264_settings(height: int | None) -> tuple[str, str]:
    """``(preset, crf)`` for the watermark-only pass on a source this tall."""
    if height is not None:
        for max_height, preset, crf in _WATERMARK_X264_LADDER:
            if height <= max_height:
                return preset, crf
    return _WATERMARK_X264_FALLBACK


def _resolve_watermark(
    source: Path,
    *,
    job_id: str,
    target_height: int | None = None,
    size: tuple[int, int] | None = None,
) -> VideoWatermark | None:
    """Badge sized for what ``source`` will become, or ``None``.

    ``target_height`` is the transcode's cap; omit it for a source-resolution
    file. ``size`` skips the probe when the caller has already read it. ``None``
    when the watermark is off or the size cannot be read - a download is never
    failed over a missing badge.
    """
    enabled, hostname = _watermark_config()
    if not enabled:
        return None

    if size is None:
        size = _probe_video_size(source, job_id=job_id)
    if size is None:
        return None
    if target_height is not None:
        size = _scaled_size(size, target_height)

    return build_watermark(video_width=size[0], video_height=size[1], hostname=hostname)


def _stream_codecs(path: Path, *, job_id: str | None = None) -> tuple[str, str]:
    """``(video_codec, audio_codec)`` of the first stream of each kind.

    Empty strings where a stream is absent or unreadable; the callers treat
    "unknown" as "not compatible", which costs a re-encode rather than
    shipping a file that silently will not play.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name",
        "-of",
        "json",
        str(path),
    ]
    try:
        data = json.loads(_run_cmd(cmd, timeout=20, capture_stdout=True, job_id=job_id) or "{}")
    except (JobCancelledError, ShutdownError):
        raise
    except Exception as exc:
        logger.warning("Could not read stream codecs of %s: %s", path, exc)
        return "", ""

    codecs = {"video": "", "audio": ""}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind in codecs and not codecs[kind]:
            codecs[kind] = str(stream.get("codec_name") or "")
    return codecs["video"], codecs["audio"]


def _finalize_video_download(
    job_id: str,
    video: Path,
    *,
    compatible_output: bool,
    transcode_timeout_seconds: int,
) -> Path:
    """Apply the watermark and/or the compatibility promise to a "max" file.

    "max" downloads and remuxes without an encoder, so this is the one pass
    that can run afterwards - and the one place a max download can lose
    quality. It therefore does the least work that satisfies both settings:

    * nothing at all when the file already is what was asked for (the common
      case: the H.264/AAC rendition was picked at download time),
    * audio only (``-c:v copy``) when just the audio codec is incompatible,
    * a video encode when the watermark has to be burned in or the video codec
      is incompatible - never twice, the overlay rides along in that pass.

    Returns the finished file, which may have a new suffix when the
    compatibility promise forced the MP4 container.
    """
    size = _probe_video_size(video, job_id=job_id)
    # Probed once here and handed to the badge (which sizes itself in output
    # pixels) and the encoder ladder below.
    watermark = _resolve_watermark(video, job_id=job_id, size=size)
    video_codec, audio_codec = _stream_codecs(video, job_id=job_id)

    needs_video_encode = watermark is not None or (
        compatible_output and video_codec not in _COMPATIBLE_VIDEO_CODECS
    )
    # A file with no audio track needs no audio work either.
    needs_audio_encode = bool(audio_codec) and compatible_output and (
        audio_codec not in _COMPATIBLE_AUDIO_CODECS
    )
    if not needs_video_encode and not needs_audio_encode:
        return video

    messages = []
    if watermark is not None:
        messages.append("watermark")
    if (needs_video_encode and watermark is None) or needs_audio_encode:
        messages.append("compatibility")
    message = f"Applying {' and '.join(messages)}"

    preset, crf = _watermark_x264_settings(size[1] if size else None)
    duration_seconds = _probe_media(video, "video", job_id=job_id)[2]
    _transition_worker_status(job_id, "transcoding", "Waiting for transcode slot", progress=0, eta_seconds=None)

    # MP4 whenever the promise is on; otherwise the streams stay in the
    # container they already live in, because only the video is touched.
    suffix = ".mp4" if compatible_output else video.suffix
    out = video.with_name(f"{video.stem}.finalized{suffix}")
    video_args = (
        ["-c:v", "libx264", "-preset", preset, "-crf", crf]
        if needs_video_encode
        else ["-c:v", "copy"]
    )
    audio_args = ["-c:a", "aac", "-b:a", _COMPATIBLE_AAC_BITRATE] if needs_audio_encode else ["-c:a", "copy"]

    try:
        with governor.transcode_semaphore_sync:
            _check_cancellation(job_id)
            _check_shutdown()
            _transition_worker_status(job_id, "transcoding", message, progress=0, eta_seconds=None)
            _run_ffmpeg_transcode(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video),
                    *(["-i", str(watermark.path)] if watermark else []),
                    *video_filter_args(watermark),
                    *video_args,
                    *audio_args,
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(out),
                ],
                timeout=transcode_timeout_seconds,
                job_id=job_id,
                message=message,
                duration_seconds=duration_seconds,
            )
        _check_cancellation(job_id)
        # ffmpeg exiting 0 is not proof that it wrote a usable file, and the
        # input here is a verified download: never replace it with something
        # unchecked.
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError(f"Post-processing produced no usable output: {out}")
        # Identical to `video` when the container did not change.
        final = video.with_name(f"{video.stem}{suffix}")
        # Rename into place first, drop the old container second: an
        # interruption between the two leaves a usable file either way.
        out.replace(final)
        if final != video:
            video.unlink(missing_ok=True)
        return final
    except (JobCancelledError, ShutdownError):
        raise
    except Exception:
        # The watermark is cosmetic and the compatibility promise is a
        # convenience; the downloaded media is neither. Keep the file rather
        # than failing a job that already has what the user asked for.
        logger.warning(
            "Could not post-process %s (%s); keeping the untouched download", video, message, exc_info=True
        )
        return video
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", out, exc)


def _build_ytdlp_cmd(
    url: str,
    output_template: str,
    *,
    media_type: str,
    quality: str,
    lossless_audio: bool = False,
) -> list[str]:
    """Build a yt-dlp command for the requested media type and quality.

    ``quality`` is ``"max"`` for best quality, otherwise a capped transcode
    path. ``lossless_audio`` downloads the source audio without re-encoding.
    """
    concurrent_fragments, compatible_output = _download_tuning()
    runtime_limits = _download_runtime_limits()
    cmd = [
        "yt-dlp",
        "--no-playlist",
        # Progress on discrete lines instead of one carriage-return-rewritten
        # line, so it survives being read from a pipe (see _DownloadProgress).
        "--newline",
        "--progress-template",
        _YTDLP_PROGRESS_TEMPLATE,
        "--max-filesize",
        runtime_limits.max_filesize_arg,
        # Parallel fragment downloads for DASH/HLS sources; ignored for
        # progressive single-file downloads.
        "--concurrent-fragments", str(concurrent_fragments),
        # Fast-fail: a blocked source must not tie up a worker slot in yt-dlp's
        # default retry storm (10 download + 10 fragment retries with backoff).
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "3",
        "--extractor-retries", "1",
        *_cookies_args_for_url(url),
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        output_template,
    ]
    if media_type == "audio":
        if lossless_audio:
            # Best audio, no re-encode - keeps the original codec.
            cmd.extend(["-f", "ba/b", "-x"])
        else:
            cmd.extend(["-f", "ba/b", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"])
    elif quality == "max":
        cmd.extend(["-f", "bv*+ba/b"])
        if compatible_output:
            # The cheap half of the compatibility promise: sorting vcodec ahead
            # of resolution picks the 1080p H.264 rendition over a 2160p
            # VP9/AV1 one, so the file needs no encoder at all. Only a source
            # with no H.264 rendition falls through to the transcode in
            # _finalize_video_download().
            cmd.extend([
                "-S", _COMPATIBLE_FORMAT_SORT,
                "--merge-output-format", "mp4",
                # Lossless: rewraps into MP4 only if the merge picked another
                # container, and is a no-op when it did not.
                "--remux-video", "mp4",
            ])
        # Without the promise there is deliberately no --merge-output-format:
        # yt-dlp then keeps the streams in the container they belong in
        # (.webm for VP9/Opus, .mkv for AV1), so the download stays a pure
        # remux and the extension does not lie about what is inside.
    else:
        cmd.extend(["-f", "bv*[height<=720]+ba/b", "--merge-output-format", "mp4"])
    cmd.append("--")
    cmd.append(url)
    return cmd


def _find_audio_source(job_dir: Path, stem: str) -> Path | None:
    """Find the lossless audio source; its extension depends on the source."""
    for ext in AUDIO_SOURCE_EXTENSIONS:
        source = job_dir / f"{stem}.source{ext}"
        if source.is_file():
            return source
    for candidate in job_dir.glob(f"{stem}.source.*"):
        if candidate.suffix.lower() in AUDIO_SOURCE_EXTENSIONS:
            return candidate
    return None


def _find_video_source(job_dir: Path, stem: str, *, marker: str = "") -> Path | None:
    pattern = f"{stem}{marker}.*"
    for candidate in sorted(job_dir.glob(pattern)):
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() in _VIDEO_SOURCE_EXTENSIONS:
            return candidate
    return None


def _download_media(job_id: str, url: str, *, quality: str, media_type: str) -> Path:
    _check_cancellation(job_id)
    _check_shutdown()
    job_dir = _get_base_dir() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    clean_url = normalize_info_url(url)
    stem = _build_output_stem(job_id, clean_url, quality, media_type)
    if media_type == "audio":
        audio_message = "Downloading audio (lossless)"
        _transition_worker_status(job_id, "downloading", audio_message)
        cmd = _build_ytdlp_cmd(
            clean_url,
            str(job_dir / f"{stem}.source.%(ext)s"),
            media_type=media_type,
            quality=quality,
            lossless_audio=True,
        )
        _check_cancellation(job_id)
        _run_ytdlp_download(cmd, job_id=job_id, message=audio_message)
        _check_cancellation(job_id)
        _rename_thumbnail(job_dir)

        source_file = _find_audio_source(job_dir, stem)
        if source_file is None:
            raise RuntimeError(f"Audio source file not found in {job_dir}")

        return source_file

    if quality == "max":
        # Read once for the whole job: the format selection and the
        # post-processing pass must agree on the promise, and a setting changed
        # mid-download must not give the two halves different orders.
        _, compatible_output = _download_tuning()
        transcode_timeout = _download_runtime_limits().transcode_timeout_seconds
        max_message = "Downloading best video+audio"
        _transition_worker_status(job_id, "downloading", max_message)
        cmd = _build_ytdlp_cmd(
            clean_url,
            str(job_dir / f"{stem}.%(ext)s"),
            media_type=media_type,
            quality=quality,
        )
        _check_cancellation(job_id)
        _run_ytdlp_download(cmd, job_id=job_id, message=max_message)
        _check_cancellation(job_id)
        _rename_thumbnail(job_dir)

        downloaded = job_dir / f"{stem}.mp4"
        if not downloaded.is_file():
            # Without the compatibility promise the container is whatever the
            # streams belong in (.webm, .mkv), so the name is not known ahead.
            found_out = _find_video_source(job_dir, stem)
            if found_out is None:
                raise RuntimeError(f"Downloaded video file not found in {job_dir}")
            downloaded = found_out

        return _finalize_video_download(
            job_id,
            downloaded,
            compatible_output=compatible_output,
            transcode_timeout_seconds=transcode_timeout,
        )

    out = job_dir / f"{stem}.mp4"

    _check_cancellation(job_id)
    source_message = "Downloading source for transcoding"
    _transition_worker_status(job_id, "downloading", source_message)
    _run_ytdlp_download(
        _build_ytdlp_cmd(
            clean_url,
            str(job_dir / f"{stem}.source.%(ext)s"),
            media_type=media_type,
            quality=quality,
        ),
        job_id=job_id,
        message=source_message,
    )
    _rename_thumbnail(job_dir)

    source_video = _find_video_source(job_dir, stem, marker=".source")
    if source_video is None:
        raise RuntimeError(f"Video source file not found in {job_dir}")

    _check_cancellation(job_id)
    _check_shutdown()
    target_height = 720 if quality == "medium" else 480
    scale = f"scale=-2:'min({target_height},ih)'"  # cap height, never upscale
    transcode_message = f"Transcoding to {quality}"
    _, _, source_duration_seconds = _probe_media(source_video, media_type, job_id=job_id)
    # Folded into the transcode below rather than a pass of its own.
    watermark = _resolve_watermark(source_video, job_id=job_id, target_height=target_height)
    _transition_worker_status(
        job_id,
        "transcoding",
        "Waiting for transcode slot",
        progress=0,
        eta_seconds=None,
    )

    try:
        with governor.transcode_semaphore_sync:
            _check_cancellation(job_id)
            _check_shutdown()
            _transition_worker_status(job_id, "transcoding", transcode_message, progress=0, eta_seconds=None)
            _run_ffmpeg_transcode(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source_video),
                    *(["-i", str(watermark.path)] if watermark else []),
                    *video_filter_args(watermark, scale=scale),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(out),
                ],
                timeout=_download_runtime_limits().transcode_timeout_seconds,
                job_id=job_id,
                message=transcode_message,
                duration_seconds=source_duration_seconds,
            )
        _check_cancellation(job_id)
    finally:
        try:
            source_video.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", source_video, exc)

    return out


def _get_filesize(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except Exception:
        return None


def _title_from_output_name(path: Path) -> str | None:
    stem = path.stem
    if stem.endswith(".source"):
        stem = stem[:-7]
    title = re.sub(r"\s\((?:max|medium|small|best|default|\w+)Quality\)$", "", stem)
    title = title.strip()
    return title or None


def _probe_media(
    path: Path,
    media_type: str,
    *,
    job_id: str | None = None,
) -> tuple[str | None, int | None, float | None]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,bit_rate:format=bit_rate,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        output = _run_cmd(cmd, timeout=20, capture_stdout=True, job_id=job_id)
        data = json.loads(output or "{}")
    except (JobCancelledError, ShutdownError):
        raise
    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None, None, None

    streams = data.get("streams", [])
    target_kind = "audio" if media_type == "audio" else "video"
    selected_stream = next(
        (s for s in streams if s.get("codec_type") == target_kind),
        None,
    )

    codec = (selected_stream or {}).get("codec_name")

    bitrate_raw = (selected_stream or {}).get("bit_rate")
    if not bitrate_raw:
        bitrate_raw = (data.get("format") or {}).get("bit_rate")

    bitrate_kbps: int | None = None
    try:
        if bitrate_raw is not None:
            bitrate_kbps = max(1, int(int(bitrate_raw) / 1000))
    except (TypeError, ValueError):
        bitrate_kbps = None

    duration_seconds: float | None = None
    try:
        duration_raw = (data.get("format") or {}).get("duration")
        if duration_raw is not None:
            # Rounded, not truncated: a 213.4 s track stored as 213 made the
            # trim guard reject a legitimate full-length selection.
            rounded = round_seconds(float(duration_raw))
            if rounded is not None:
                duration_seconds = max(1.0, rounded)
    except (TypeError, ValueError):
        duration_seconds = None

    return codec, bitrate_kbps, duration_seconds


def process_job(job: Job) -> None:
    """Download, transcode, probe metadata, and optionally queue audio analysis."""
    job_id, url, type_, quality = job

    try:
        _check_cancellation(job_id)
        _check_shutdown()
        # _download_media() owns the "downloading" transition for every path.
        out_path = _download_media(job_id, url, quality=quality, media_type=type_)

        filesize_bytes = _get_filesize(out_path)
        codec, bitrate_kbps, duration_seconds = _probe_media(out_path, type_, job_id=job_id)
        video_title = _title_from_output_name(out_path)
        # An empty probe must not erase the runtime the source reported at
        # submit time (see db.py::insert_job).
        measured: dict[str, Any] = {}
        if duration_seconds is not None:
            measured["duration_seconds"] = duration_seconds

        status = "done"
        message = "Finished"
        if type_ == "audio":
            _check_cancellation(job_id)
            _check_shutdown()
            transitioned = _transition_if_processing(
                job_id,
                "analysis",
                "Queued for audio analysis",
                filename=str(out_path),
                filesize_bytes=filesize_bytes,
                codec=codec,
                bitrate_kbps=bitrate_kbps,
                video_title=video_title,
                **measured,
            )
            if not transitioned:
                return

            analysis_result = submit_analysis(
                job_id,
                out_path,
                duration_seconds=duration_seconds,
            )
            if analysis_result is SubmitResult.QUEUE_FULL:
                logger.info("Audio analysis for %s deferred because the queue is full", job_id)
            elif analysis_result is SubmitResult.REJECTED_SHUTDOWN:
                logger.info("Audio analysis for %s remains pending during shutdown", job_id)
            return

        _check_cancellation(job_id)
        _check_shutdown()
        _transition_if_processing(
            job_id,
            status,
            message,
            filename=str(out_path),
            filesize_bytes=filesize_bytes,
            codec=codec,
            bitrate_kbps=bitrate_kbps,
            video_title=video_title,
            **measured,
        )

    except JobCancelledError:
        logger.info("Job %s was cancelled", job_id)
        _transition(job_id, "cancelled", "Job was cancelled by user")
    except ShutdownError:
        # Graceful shutdown - keep the job queued for the next startup.
        logger.info("Job %s interrupted by shutdown", job_id)
        update_job_if_status(job_id, ("processing", "downloading", "transcoding"), status="queued")
        raise
    except Exception as exc:
        err_msg = _user_facing_error(url, exc)
        logger.exception("Job %s failed", job_id)
        _transition_if_processing(job_id, "error", err_msg)
    finally:
        with _cancel_lock:
            _cancelled_jobs.discard(job_id)


def worker() -> None:
    """Worker thread loop."""
    q = get_job_queue()
    while True:
        # Do not drain queued jobs during shutdown. They remain persisted as
        # ``queued`` and are replayed on the next application startup.
        if _shutdown_event.is_set():
            return

        try:
            job = q.get(timeout=0.5)
        except queue.Empty:
            continue

        job_id = job[0]
        # The slot is free again the moment the job leaves the queue; from here
        # on its DB status guards against a second pickup.
        _release_queue_slot(job_id)
        try:
            if _shutdown_event.is_set():
                return

            if is_job_cancelled(job_id):
                _transition(job_id, "cancelled", "Job was cancelled by user")
                continue
            if not update_job_if_status(job_id, ("queued",), status="processing"):
                logger.info("Skipping job %s because its state changed before worker pickup", job_id)
                continue
            _emit(job_id, "processing", "Worker picked up job")
            process_job(job)
        except ShutdownError:
            logger.debug("Worker exiting due to shutdown, job %s is persisted as queued", job_id)
            return
        except Exception:
            logger.exception("Unhandled worker error for job %s", job_id)
            try:
                _transition(job_id, "error", "Internal worker error")
            except Exception:
                logger.exception("Failed to persist internal error state for job %s", job_id)
        finally:
            with _cancel_lock:
                _cancelled_jobs.discard(job_id)
            q.task_done()


def start_workers(n: int | None = None) -> None:
    """Start the background worker threads (count auto-detected via Governor)."""
    global _workers_started

    get_job_queue()
    if n is None:
        n = governor.worker_count

    with _worker_lock:
        if _workers_started:
            _worker_threads[:] = [thread for thread in _worker_threads if thread.is_alive()]
            if _worker_threads:
                return
            _workers_started = False

        _shutdown_event.clear()
        for _ in range(n):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            _worker_threads.append(t)

        logger.info("Started %d worker threads (effective CPUs: %.2f)", n, governor.effective_cpus)

        for platform, filename in PLATFORM_COOKIE_FILENAMES.items():
            resolved = _resolve_cookie_file(platform)
            if resolved.is_file():
                logger.info("Cookie file for %s: %s", platform, resolved)
            else:
                logger.info("No cookie file for %s (looked for %s)", platform, filename)

        _workers_started = True


def stop_workers(timeout: float = 5.0) -> None:
    """Signal workers to shut down and wait for them to finish."""
    global _workers_started

    _shutdown_event.set()
    with _worker_lock:
        threads = list(_worker_threads)

    # One shared deadline, not a per-thread slice: all threads watch the same
    # shutdown event, so total stop time stays O(timeout), not O(n * timeout).
    deadline = time.monotonic() + max(0.0, timeout)
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            logger.warning("Worker thread %s did not stop cleanly", t.name)

    with _worker_lock:
        # Threads that outlived the join are still running and still watching
        # _shutdown_event. Dropping them here would hide them from
        # start_workers(), which would then clear that event and start a second
        # set alongside them - two workers on one job directory. Keeping them
        # makes the existing "already started" early return do its job.
        _worker_threads[:] = [thread for thread in _worker_threads if thread.is_alive()]
        _workers_started = bool(_worker_threads)
