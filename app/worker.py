#!/usr/bin/env python3
#
# app/worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from .analysis_worker import SubmitResult, submit_analysis
from .utils.duration import round_seconds
from .db import get_settings, update_job, update_job_if_status, utc_timestamp
from .governor import governor
from .utils.cookies import default_cookie_file, find_cookie_file
from .utils.platform import PLATFORM_COOKIE_FILENAMES, detect_platform
from .utils.fs import AUDIO_SOURCE_EXTENSIONS, get_data_dir
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
        return Path("")

    cookie_path = find_cookie_file(filename)
    if cookie_path is None:
        return default_cookie_file(filename)

    try:
        if cookie_path.stat().st_mode & 0o077:
            logger.warning("Cookie file %s is group/world accessible", cookie_path)
    except OSError:
        logger.warning("Could not inspect permissions for cookie file %s", cookie_path)
    return cookie_path


def _cookies_args_for_url(url: str) -> list[str]:
    """Return platform-specific cookie arguments when a cookie file exists."""
    platform = detect_platform(url)
    if not platform:
        return []

    cookie_path = _resolve_cookie_file(platform)
    if cookie_path.is_file():
        return ["--cookies", str(cookie_path)]
    return []



# Queue maxsize managed by Governor for backpressure.
# Use get_job_queue() to access the queue (lazy initialization).
_job_queue: queue.Queue[Job] | None = None
_queue_lock = threading.Lock()


def get_job_queue() -> queue.Queue[Job]:
    """Get the job queue with Governor-managed maxsize for backpressure."""
    global _job_queue
    with _queue_lock:
        if _job_queue is None:
            maxsize = governor.queue_maxsize
            _job_queue = queue.Queue(maxsize=maxsize)
            logger.info("Job queue initialized with maxsize=%d (backpressure %s)",
                       maxsize, "enabled" if maxsize > 0 else "disabled")
        return _job_queue


_status_callback: StatusCallback | None = None
_workers_started = False
_worker_lock = threading.Lock()
_shutdown_event = threading.Event()
_worker_threads: list[threading.Thread] = []
_cancel_lock = threading.Lock()
_cancelled_jobs: set[str] = set()
_active_lock = threading.Lock()
_active_processes: dict[str, subprocess.Popen[str]] = {}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Non-positive %s=%r; using default %d", name, raw, default)
        return default
    return value

_TIMEOUT_DOWNLOAD: Final = _env_int("WORKER_TIMEOUT_DL", 3600)
_TIMEOUT_TRANSCODE: Final = _env_int("WORKER_TIMEOUT_TC", 7200)
# yt-dlp accepts human-readable sizes such as ``4G``. Keep a safe default
# while allowing deployments with larger managed volumes to raise the limit.
_MAX_DOWNLOAD_FILESIZE: Final = os.environ.get("WORKER_MAX_FILESIZE", "4G").strip() or "4G"
_COMMAND_POLL_INTERVAL: Final = 1.0
# Bounds for the user-configurable --concurrent-fragments value. The API
# clamps to the same range; this guard also covers values written directly
# into the settings table.
_MIN_CONCURRENT_FRAGMENTS: Final = 1
_MAX_CONCURRENT_FRAGMENTS: Final = 16
_DEFAULT_CONCURRENT_FRAGMENTS: Final = 3
# Mirrors the "download_mp4_preset" default in app/db.py.
_DEFAULT_MP4_PRESET: Final = True


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
        cookie_path = _resolve_cookie_file(platform or "") if platform else Path("")
        if platform and cookie_file and not cookie_path.is_file():
            return (
                f"{platform.capitalize()} requires authentication. "
                f"Place a Netscape-format cookie file at "
                f"data/{cookie_file} or set FETCHLY_COOKIES_DIR."
            )
        return f"Platform requires authentication: {raw[:200]}"

    return f"Job failed: {raw[:300]}"


def _now_iso() -> str:
    """Timestamp for status-event payloads (informational; not persisted)."""
    return datetime.now(UTC).isoformat()


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


def is_job_cancelled(job_id: str) -> bool:
    """Check if a job has been marked for cancellation."""
    with _cancel_lock:
        return job_id in _cancelled_jobs


def _check_cancellation(job_id: str) -> None:
    """Raise if job was cancelled."""
    if is_job_cancelled(job_id):
        raise JobCancelledError(f"Job {job_id} was cancelled")


def _check_shutdown() -> None:
    """Raise if workers are shutting down."""
    if _shutdown_event.is_set():
        raise ShutdownError("Shutdown requested")


def _emit(job_id: str, status: str, message: str = "", **extra: Any) -> None:
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
        logger.warning("Status callback failed for %s: %s", job_id, exc, exc_info=True)


def _transition(job_id: str, status: str, message: str = "", **extra: Any) -> None:
    """Persist a job state change and emit the matching status event."""
    update_job(job_id, status=status, message=message, **extra)
    _emit(job_id, status, message, **extra)


def _transition_if_processing(job_id: str, status: str, message: str = "", **extra: Any) -> bool:
    """Persist a terminal update only if the job is still in a worker-owned state."""
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
    """Persist worker-owned in-flight statuses and emit matching status events."""
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
    return False


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Send a signal to a child process group created with start_new_session."""
    if proc.poll() is not None:
        return

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    except PermissionError:
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.debug("Permission denied sending signal to subprocess pid=%s", proc.pid)


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
    proc: subprocess.Popen[str] | None = None
    stderr_tmp: tempfile._TemporaryFileWrapper[str] | None = None
    try:
        stderr_tmp = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", errors="replace", delete=True)
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
                        else:
                            proc.wait(timeout=poll_timeout)
                    except subprocess.TimeoutExpired:
                        continue

                if job_id is not None:
                    _check_cancellation(job_id)
                _check_shutdown()

                if proc.returncode != 0:
                    executable = cmd[0] if cmd else "command"
                    stderr_tail = ""
                    if stderr_tmp is not None:
                        try:
                            stderr_tmp.seek(0)
                            stderr_text = stderr_tmp.read()
                            stderr_tail = stderr_text[-800:].strip()
                        except Exception:
                            stderr_tail = ""

                    if stderr_tail:
                        logger.warning("%s failed with exit code %s. stderr tail: %s", executable, proc.returncode, stderr_tail)
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
        logger.error("Command failed: %s", " ".join(cmd), exc_info=True)
        raise RuntimeError("Command execution failed") from exc
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


def _read_progress_lines(pipe: Any, target_queue: queue.Queue[str | None]) -> None:
    try:
        for raw_line in iter(pipe.readline, ""):
            try:
                target_queue.put(raw_line.rstrip(), timeout=0.1)
            except queue.Full:
                continue
    finally:
        try:
            target_queue.put(None, timeout=0.1)
        except queue.Full:
            pass


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

    pct = max(0, min(100, int(round((out_seconds / duration_seconds) * 100))))
    if pct <= last_progress:
        return last_progress

    eta_seconds: int | None = None
    if 0 < pct < 100:
        elapsed = max(0.0, time.monotonic() - started_at)
        total_estimate = elapsed / (pct / 100.0)
        eta_seconds = max(0, int(round(total_estimate - elapsed)))

    _emit(job_id, "transcoding", message, progress=pct, eta_seconds=eta_seconds)
    return pct


def _run_ffmpeg_transcode(
    cmd: list[str],
    *,
    timeout: int,
    job_id: str,
    message: str,
    duration_seconds: float | None,
) -> None:
    logger.debug("Executing ffmpeg with progress: %s", " ".join(cmd))
    _check_cancellation(job_id)
    _check_shutdown()

    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    progress_lines: queue.Queue[str | None] = queue.Queue(maxsize=500)
    reader_thread: threading.Thread | None = None
    progress_state: dict[str, str] = {}
    last_progress = -1

    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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
                    if "=" not in line:
                        continue

                    key, value = line.split("=", 1)
                    progress_state[key] = value
                    if key != "progress":
                        continue

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

                _check_cancellation(job_id)
                _check_shutdown()

                if proc.returncode != 0:
                    raise RuntimeError(f"Command failed: {proc.returncode}")

                if duration_seconds and last_progress < 100:
                    _emit(job_id, "transcoding", message, progress=100, eta_seconds=0)
            except Exception:
                if proc.poll() is None:
                    _terminate_process(proc)
                raise
    except (JobCancelledError, ShutdownError, RuntimeError):
        raise
    except Exception as exc:
        if _shutdown_event.is_set():
            raise ShutdownError("Shutdown requested") from exc
        logger.error("Command failed: %s", " ".join(cmd), exc_info=True)
        raise RuntimeError("Command execution failed") from exc
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if reader_thread is not None:
            reader_thread.join(timeout=0.2)
        if proc is not None:
            _unregister_active_process(job_id, proc)


_WIN_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def sanitize_filename(name: str, max_len: int = 120) -> str:
    # Strip control characters (0x00-0x1F, 0x7F) and Windows-reserved chars
    cleaned = re.sub(r"[\x00-\x1f\x7f\\/:*?\"<>|\[\]]", "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.upper() in _WIN_RESERVED_NAMES:
        cleaned = "video"
    return cleaned[:max_len].rstrip(" .") or "video"


def _quality_label(quality: str) -> str:
    return _QUALITY_LABELS.get((quality or "").lower(), f"{quality}Quality" if quality else "defaultQuality")


def _build_output_stem(job_id: str, url: str, quality: str, media_type: str) -> str:
    try:
        title_raw = _run_cmd(
            ["yt-dlp", "--no-playlist", *_cookies_args_for_url(url), "--get-title", "--", url],
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


def _download_tuning() -> tuple[int, bool]:
    """Read the user-configured yt-dlp download tuning from the settings store.

    Returns ``(concurrent_fragments, mp4_preset)``. Both are set through
    ``POST /api/settings``. A failing settings read must not abort an
    otherwise valid download, so the documented defaults are used and the
    failure is logged.
    """
    try:
        settings = get_settings()
    except Exception:
        logger.warning("Could not read download settings; falling back to defaults", exc_info=True)
        return _DEFAULT_CONCURRENT_FRAGMENTS, _DEFAULT_MP4_PRESET

    fragments = settings.get("download_concurrent_fragments", _DEFAULT_CONCURRENT_FRAGMENTS)
    clamped = min(max(int(fragments), _MIN_CONCURRENT_FRAGMENTS), _MAX_CONCURRENT_FRAGMENTS)
    return clamped, bool(settings.get("download_mp4_preset", _DEFAULT_MP4_PRESET))


def _build_ytdlp_cmd(
    url: str,
    output_template: str,
    *,
    media_type: str,
    quality: str,
    lossless_audio: bool = False,
) -> list[str]:
    """Build a yt-dlp command for the requested media type and quality.

    Args:
        url: Normalized video URL.
        output_template: yt-dlp output template including ``%(ext)s``.
        media_type: ``"audio"`` or ``"video"``.
        quality: ``"max"`` for best quality, otherwise a capped transcode path.
        lossless_audio: When True, download the source audio without re-encoding.
    """
    concurrent_fragments, mp4_preset = _download_tuning()
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize",
        _MAX_DOWNLOAD_FILESIZE,
        # Parallel fragment downloads for DASH/HLS sources; ignored for
        # progressive single-file downloads.
        "--concurrent-fragments", str(concurrent_fragments),
        # Fast-fail limits: keep a blocked/unavailable source (e.g. login-walled
        # Instagram reel) from tying up a worker slot in yt-dlp's default retry
        # storm (10 download + 10 fragment retries with backoff).
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
            # Download best audio without re-encoding - keeps original codec (opus/m4a/etc)
            cmd.extend(["-f", "ba/b", "-x"])
        else:
            cmd.extend(["-f", "ba/b", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"])
    elif quality == "max":
        cmd.extend(["-f", "bv*+ba/b"])
        if mp4_preset:
            # yt-dlp's `-t mp4` preset expands to --merge-output-format mp4
            # --remux-video mp4 -S vcodec:h264,...,acodec:aac. Since vcodec
            # sorts ahead of res, this prefers a 1080p H.264 rendition over a
            # 2160p VP9/AV1 one - the trade the default makes for a file that
            # plays in every browser and on every device (see db.py).
            cmd.extend(["-t", "mp4"])
        else:
            cmd.extend(["--merge-output-format", "mp4"])
    else:
        cmd.extend(["-f", "bv*[height<=720]+ba/b", "--merge-output-format", "mp4"])
    cmd.append("--")
    cmd.append(url)
    return cmd


def _find_audio_source(job_dir: Path, stem: str) -> Path | None:
    """Find the lossless audio source file in the job directory.
    
    yt-dlp outputs audio in various formats (opus, m4a, webm, etc.) depending on source.
    This function finds the actual downloaded file matching the stem pattern.
    """
    # Common audio extensions from YouTube
    for ext in AUDIO_SOURCE_EXTENSIONS:
        source = job_dir / f"{stem}.source{ext}"
        if source.is_file():
            return source
    # Fallback: search for any .source.* audio file
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

    # Normalize URL to strip playlist params that cause issues
    clean_url = normalize_info_url(url)
    stem = _build_output_stem(job_id, clean_url, quality, media_type)
    if media_type == "audio":
        # Download lossless audio (no transcode) - we'll convert to MP3 on download
        _transition_worker_status(job_id, "downloading", "Downloading audio (lossless)")
        cmd = _build_ytdlp_cmd(
            clean_url,
            str(job_dir / f"{stem}.source.%(ext)s"),
            media_type=media_type,
            quality=quality,
            lossless_audio=True,
        )
        _check_cancellation(job_id)
        _run_cmd(cmd, timeout=_TIMEOUT_DOWNLOAD, job_id=job_id)
        _check_cancellation(job_id)
        _rename_thumbnail(job_dir)
        
        # Find the actual downloaded file (extension varies by source)
        source_file = _find_audio_source(job_dir, stem)
        if source_file is None:
            raise RuntimeError(f"Audio source file not found in {job_dir}")
        
        return source_file

    if quality == "max":
        cmd = _build_ytdlp_cmd(
            clean_url,
            str(job_dir / f"{stem}.%(ext)s"),
            media_type=media_type,
            quality=quality,
        )
        _check_cancellation(job_id)
        _run_cmd(cmd, timeout=_TIMEOUT_DOWNLOAD, job_id=job_id)
        _check_cancellation(job_id)
        _rename_thumbnail(job_dir)

        expected_out = job_dir / f"{stem}.mp4"
        if expected_out.is_file():
            return expected_out

        found_out = _find_video_source(job_dir, stem)
        if found_out is None:
            raise RuntimeError(f"Downloaded video file not found in {job_dir}")
        return found_out

    out = job_dir / f"{stem}.mp4"

    _check_cancellation(job_id)
    _transition_worker_status(job_id, "downloading", "Downloading source for transcoding")
    _run_cmd(
        _build_ytdlp_cmd(
            clean_url,
            str(job_dir / f"{stem}.source.%(ext)s"),
            media_type=media_type,
            quality=quality,
        ),
        timeout=_TIMEOUT_DOWNLOAD,
        job_id=job_id,
    )
    _rename_thumbnail(job_dir)

    source_video = _find_video_source(job_dir, stem, marker=".source")
    if source_video is None:
        raise RuntimeError(f"Video source file not found in {job_dir}")

    _check_cancellation(job_id)
    _check_shutdown()
    # Cap resolution without upscaling: min(target, input_height)
    target_height = 720 if quality == "medium" else 480
    scale = f"scale=-2:'min({target_height},ih)'"
    transcode_message = f"Transcoding to {quality}"
    _, _, source_duration_seconds = _probe_media(source_video, media_type, job_id=job_id)
    _transition_worker_status(
        job_id,
        "transcoding",
        "Waiting for transcode slot",
        progress=0,
        eta_seconds=None,
    )
    
    try:
        # Use Governor semaphore to limit concurrent transcoding (CPU/memory protection)
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
                    "-vf",
                    scale,
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
                timeout=_TIMEOUT_TRANSCODE,
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
        if type_ == "audio":
            _transition_worker_status(job_id, "downloading", "Downloading audio stream")
        elif quality == "max":
            _transition_worker_status(job_id, "downloading", "Downloading best video+audio")

        out_path = _download_media(job_id, url, quality=quality, media_type=type_)

        filesize_bytes = _get_filesize(out_path)
        codec, bitrate_kbps, duration_seconds = _probe_media(out_path, type_, job_id=job_id)
        video_title = _title_from_output_name(out_path)

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
                duration_seconds=duration_seconds,
                video_title=video_title,
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
            finished_at=utc_timestamp(),
            filesize_bytes=filesize_bytes,
            codec=codec,
            bitrate_kbps=bitrate_kbps,
            duration_seconds=duration_seconds,
            video_title=video_title,
        )

    except JobCancelledError:
        logger.info("Job %s was cancelled", job_id)
        _transition(job_id, "cancelled", "Job was cancelled by user", finished_at=utc_timestamp())
    except ShutdownError:
        # Graceful shutdown - keep the job queued for the next startup.
        logger.info("Job %s interrupted by shutdown", job_id)
        update_job_if_status(job_id, ("processing", "downloading", "transcoding"), status="queued")
        raise
    except Exception as exc:
        err_msg = _user_facing_error(url, exc)
        logger.exception("Job %s failed", job_id)
        _transition_if_processing(job_id, "error", err_msg, finished_at=utc_timestamp())
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
        try:
            if _shutdown_event.is_set():
                return

            if is_job_cancelled(job_id):
                _transition(job_id, "cancelled", "Job was cancelled by user", finished_at=utc_timestamp())
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
                _transition(job_id, "error", "Internal worker error", finished_at=utc_timestamp())
            except Exception:
                logger.exception("Failed to persist internal error state for job %s", job_id)
        finally:
            with _cancel_lock:
                _cancelled_jobs.discard(job_id)
            q.task_done()


def start_workers(n: int | None = None) -> None:
    """Start the background worker threads.
    
    Args:
        n: Number of workers. If None, auto-detect via Governor.
    """
    global _workers_started
    
    # Initialize queue and get worker count from Governor
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

        # Log cookie file availability at startup for operational visibility.
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

    # Divide timeout among threads to avoid O(n * timeout) worst case
    per_thread_timeout = max(0.5, timeout / max(len(threads), 1))
    for t in threads:
        t.join(timeout=per_thread_timeout)
        if t.is_alive():
            logger.warning("Worker thread %s did not stop cleanly", t.name)

    with _worker_lock:
        _worker_threads.clear()
        _workers_started = False
