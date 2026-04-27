#!/usr/bin/env python3
#
# app/worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .db import update_job
from .governor import governor

logger = logging.getLogger(__name__)

_COOKIES_PATH: Final = Path(__file__).parent.parent / "youtube_cookies.txt"
_BASE_DIR: Path | None = None
_base_dir_lock = threading.Lock()


def _get_base_dir() -> Path:
    """Lazily initialize the worker output directory."""
    global _BASE_DIR
    if _BASE_DIR is None:
        with _base_dir_lock:
            if _BASE_DIR is None:
                _BASE_DIR = (Path(__file__).parent.parent / "data").resolve()
                _BASE_DIR.mkdir(parents=True, exist_ok=True)
    return _BASE_DIR


def _cookies_args() -> list[str]:
    """Return --cookies argument list if youtube_cookies.txt exists."""
    if _COOKIES_PATH.is_file():
        return ["--cookies", str(_COOKIES_PATH)]
    return []


def _normalize_url(url: str) -> str:
    """Strip playlist parameters from YouTube URLs to avoid yt-dlp issues."""
    from urllib.parse import urlparse, parse_qs
    value = url.strip()
    try:
        parsed = urlparse(value)
    except Exception:
        return value
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host.endswith("youtube.com") and path == "/watch":
        params = parse_qs(parsed.query, keep_blank_values=False)
        video_id = (params.get("v") or [""])[0].strip()
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    if host.endswith("youtu.be"):
        segment = path.strip("/").split("/")[0].strip()
        if segment:
            return f"https://youtu.be/{segment}"
    return value

# Queue maxsize managed by Governor for backpressure
# Use get_job_queue() to access the queue (lazy initialization)
_job_queue: queue.Queue[tuple[str, str, str, str]] | None = None
_queue_lock = threading.Lock()


def get_job_queue() -> queue.Queue[tuple[str, str, str, str]]:
    """Get the job queue with Governor-managed maxsize for backpressure."""
    global _job_queue
    if _job_queue is None:
        with _queue_lock:
            if _job_queue is None:
                maxsize = governor.queue_maxsize
                _job_queue = queue.Queue(maxsize=maxsize)
                logger.info("Job queue initialized with maxsize=%d (backpressure %s)",
                           maxsize, "enabled" if maxsize > 0 else "disabled")
    return _job_queue


_status_callback: Callable[[dict[str, Any]], None] | None = None
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


_QUALITY_LABELS: Final[dict[str, str]] = {
    "max": "maxQuality",
    "medium": "mediumQuality",
    "small": "smallQuality",
    "best": "bestQuality",
}


class JobCancelledError(Exception):
    """Raised when a job is explicitly cancelled by user request."""


class ShutdownError(Exception):
    """Raised when a running command fails because the worker is shutting down."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def set_status_callback(callback: Callable[[dict[str, Any]], None] | None) -> None:
    global _status_callback
    _status_callback = callback


def cancel_job(job_id: str) -> None:
    """Mark a job for cancellation."""
    with _cancel_lock:
        _cancelled_jobs.add(job_id)
    # Best-effort: terminate currently running subprocess for immediate cancel.
    with _active_lock:
        proc = _active_processes.get(job_id)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            logger.debug("Failed to terminate active process for %s", job_id, exc_info=True)


def is_job_cancelled(job_id: str) -> bool:
    """Check if a job has been marked for cancellation."""
    with _cancel_lock:
        return job_id in _cancelled_jobs


def _check_cancellation(job_id: str) -> None:
    """Raise if job was cancelled."""
    if is_job_cancelled(job_id):
        raise JobCancelledError(f"Job {job_id} was cancelled")


def _emit(job_id: str, status: str, message: str = "", **extra: Any) -> None:
    if _status_callback is None:
        return
    payload = {
        "id": job_id,
        "status": status,
        "message": message,
        "timestamp": _now_iso(),
    }
    payload.update(extra)
    _status_callback(payload)


def _terminate_process(proc: subprocess.Popen[str], *, grace_seconds: float = 2.0) -> None:
    """Terminate a subprocess and force-kill if it does not exit in time."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_seconds)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=grace_seconds)
        except Exception:
            logger.debug("Failed to fully stop subprocess pid=%s", proc.pid, exc_info=True)


def _run_cmd(
    cmd: list[str],
    *,
    timeout: int,
    capture_stdout: bool = False,
    job_id: str | None = None,
) -> str:
    logger.debug("Executing command: %s", " ".join(cmd))
    started = time.monotonic()
    stdout_value = ""
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            # Avoid PIPE deadlocks on long-running yt-dlp/ffmpeg stderr output.
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if job_id is not None:
            with _active_lock:
                _active_processes[job_id] = proc

        while True:
            # Terminate quickly on cancellation/shutdown instead of waiting for timeout.
            if job_id is not None and is_job_cancelled(job_id):
                _terminate_process(proc)
                raise JobCancelledError(f"Job {job_id} was cancelled")
            if _shutdown_event.is_set():
                _terminate_process(proc)
                raise ShutdownError("Shutdown requested")

            elapsed = time.monotonic() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                _terminate_process(proc)
                raise RuntimeError(f"Command timed out after {timeout}s")

            try:
                stdout_value, _ = proc.communicate(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        if proc.returncode != 0:
            raise RuntimeError(f"Command failed: {proc.returncode}")
    except JobCancelledError:
        raise
    except ShutdownError:
        raise
    except RuntimeError:
        if proc is not None and proc.poll() is None:
            proc.kill()
        raise
    except Exception as exc:
        # Check if shutdown was requested (Ctrl+C or SIGTERM)
        if _shutdown_event.is_set():
            raise ShutdownError("Shutdown requested") from exc
        logger.error("Command failed: %s", " ".join(cmd), exc_info=True)
        raise RuntimeError("Command execution failed") from exc
    finally:
        if job_id is not None:
            with _active_lock:
                _active_processes.pop(job_id, None)

    return stdout_value if capture_stdout else ""


def _sanitize_filename(name: str, max_len: int = 120) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "video"
    return cleaned[:max_len].rstrip(" .") or "video"


def _quality_label(quality: str) -> str:
    return _QUALITY_LABELS.get((quality or "").lower(), f"{quality}Quality" if quality else "defaultQuality")


def _build_output_stem(url: str, quality: str) -> str:
    try:
        title_raw = _run_cmd(["yt-dlp", "--no-playlist", *_cookies_args(), "--get-title", url], timeout=120, capture_stdout=True).strip()
    except Exception as exc:
        logger.warning("Could not fetch video title, using fallback filename: %s", exc)
        title_raw = "video"
    title = _sanitize_filename(title_raw)
    return f"{title} ({_quality_label(quality)})"


def _rename_thumbnail(job_dir: Path) -> None:
    for thumb in job_dir.glob("*.jpg"):
        if thumb.name != "thumbnail.jpg":
            thumb.rename(job_dir / "thumbnail.jpg")
            break


def _build_ytdlp_cmd(
    url: str,
    output_template: str,
    *,
    media_type: str,
    quality: str,
) -> list[str]:
    """Build a yt-dlp command with shared baseline arguments."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        *_cookies_args(),
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        output_template,
    ]
    if media_type == "audio":
        cmd.extend(["-f", "ba/b", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"])
    elif quality == "max":
        cmd.extend(["-f", "bv*+ba/b", "--merge-output-format", "mp4"])
    else:
        cmd.extend(["-f", "bv*[height<=720]+ba/b", "--merge-output-format", "mp4"])
    cmd.append(url)
    return cmd


def _download_media(job_id: str, url: str, *, quality: str, media_type: str) -> Path:
    _check_cancellation(job_id)
    job_dir = _get_base_dir() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Normalize URL to strip playlist params that cause issues
    clean_url = _normalize_url(url)
    stem = _build_output_stem(clean_url, quality)
    if media_type == "audio":
        out = job_dir / f"{stem}.mp3"
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
        return out

    if quality == "max":
        out = job_dir / f"{stem}.mp4"
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
        return out

    temp = job_dir / f"{stem}.source.mp4"
    out = job_dir / f"{stem}.mp4"

    _check_cancellation(job_id)
    _emit(job_id, "downloading", "Downloading source for transcoding")
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

    _check_cancellation(job_id)
    scale = "scale=-2:720" if quality == "medium" else "scale=-2:480"
    _emit(job_id, "transcoding", f"Transcoding to {quality}")
    
    # Use Governor semaphore to limit concurrent transcoding (CPU/memory protection)
    with governor.transcode_semaphore_sync:
        _run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(temp),
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
                str(out),
            ],
            timeout=_TIMEOUT_TRANSCODE,
            job_id=job_id,
        )
    _check_cancellation(job_id)

    try:
        temp.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove temp file %s: %s", temp, exc)

    return out


def _get_filesize(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except Exception:
        return None


def _title_from_output_name(path: Path) -> str | None:
    stem = path.stem
    title = re.sub(r"\s\((?:max|medium|small|best|default|\w+)Quality\)$", "", stem)
    title = title.replace(".source", "").strip()
    return title or None


def _probe_media(path: Path, media_type: str) -> tuple[str | None, int | None, int | None]:
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
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        data = json.loads(result.stdout or "{}")
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

    duration_seconds: int | None = None
    try:
        duration_raw = (data.get("format") or {}).get("duration")
        if duration_raw is not None:
            duration_seconds = max(1, int(float(duration_raw)))
    except (TypeError, ValueError):
        duration_seconds = None

    return codec, bitrate_kbps, duration_seconds


def process_job(job: tuple[str, str, str, str]) -> None:
    """Process a single download/transcode job."""
    job_id, url, type_, quality = job

    try:
        if type_ == "audio":
            _emit(job_id, "downloading", "Downloading audio stream")
        elif quality == "max":
            _emit(job_id, "downloading", "Downloading best video+audio")

        out_path = _download_media(job_id, url, quality=quality, media_type=type_)

        filesize_bytes = _get_filesize(out_path)
        codec, bitrate_kbps, duration_seconds = _probe_media(out_path, type_)
        video_title = _title_from_output_name(out_path)

        update_job(
            job_id,
            status="done",
            filename=str(out_path),
            finished_at=_now_iso(),
            filesize_bytes=filesize_bytes,
            codec=codec,
            bitrate_kbps=bitrate_kbps,
            duration_seconds=duration_seconds,
            video_title=video_title,
        )
        _emit(
            job_id,
            "done",
            "Finished",
            filesize_bytes=filesize_bytes,
            codec=codec,
            bitrate_kbps=bitrate_kbps,
            duration_seconds=duration_seconds,
            video_title=video_title,
        )

    except JobCancelledError:
        logger.info("Job %s was cancelled", job_id)
        update_job(job_id, status="cancelled", finished_at=_now_iso(), message="Job was cancelled by user")
        _emit(job_id, "cancelled", "Job was cancelled")
        with _cancel_lock:
            _cancelled_jobs.discard(job_id)
    except ShutdownError:
        # Graceful shutdown - keep the job queued for the next startup.
        logger.info("Job %s interrupted by shutdown", job_id)
        update_job(job_id, status="queued")
        raise
    except Exception as exc:
        err_msg = f"Job failed: {exc}"
        logger.exception("Job %s failed", job_id)
        update_job(job_id, status="error", finished_at=_now_iso(), message=err_msg)
        _emit(job_id, "error", err_msg)


def worker() -> None:
    """Worker thread loop."""
    q = get_job_queue()
    while True:
        if _shutdown_event.is_set() and q.empty():
            return

        try:
            job = q.get(timeout=0.5)
        except queue.Empty:
            continue

        job_id = job[0]
        try:
            update_job(job_id, status="processing")
            _emit(job_id, "processing", "Worker picked up job")
            process_job(job)
        except ShutdownError:
            logger.debug("Worker exiting due to shutdown, requeuing %s", job_id)
            try:
                q.put(job, timeout=5.0)
            except queue.Full:
                logger.critical("Queue full, cannot requeue %s", job_id)
                update_job(job_id, status="queued", message="Requeue on shutdown failed; restart will be required")
            return
        except Exception:
            logger.exception("Unhandled worker error for job %s", job_id)
            try:
                update_job(job_id, status="error", finished_at=_now_iso())
                _emit(job_id, "error", "Internal worker error")
            except Exception:
                pass
        finally:
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
            return

        _shutdown_event.clear()
        for _ in range(n):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            _worker_threads.append(t)
        
        logger.info("Started %d worker threads (effective CPUs: %.2f)", n, governor.effective_cpus)

        _workers_started = True


def stop_workers(timeout: float = 5.0) -> None:
    """Signal workers to shut down and wait for them to finish."""
    global _workers_started

    _shutdown_event.set()
    with _worker_lock:
        threads = list(_worker_threads)

    for t in threads:
        t.join(timeout=timeout)

    with _worker_lock:
        _worker_threads.clear()
        _workers_started = False
