#!/usr/bin/env python3
#
# app/worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import update_job

logger = logging.getLogger(__name__)

BASE_DIR = (Path(__file__).parent.parent / "data").resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)

_QUEUE_MAXSIZE = max(0, int(os.environ.get("WORKER_QUEUE_MAXSIZE", "0")))
job_queue: queue.Queue[tuple[str, str, str, str]] = queue.Queue(maxsize=_QUEUE_MAXSIZE)

_status_callback: Callable[[dict[str, Any]], None] | None = None
_workers_started = False
_shutdown_event = threading.Event()
_worker_threads: list[threading.Thread] = []

_TIMEOUT_DOWNLOAD = int(os.environ.get("WORKER_TIMEOUT_DL", "3600"))
_TIMEOUT_TRANSCODE = int(os.environ.get("WORKER_TIMEOUT_TC", "7200"))


_QUALITY_LABELS = {
    "max": "maxQuality",
    "medium": "mediumQuality",
    "small": "smallQuality",
    "best": "bestQuality",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def set_status_callback(callback: Callable[[dict[str, Any]], None] | None) -> None:
    global _status_callback
    _status_callback = callback


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


def _run_cmd(cmd: list[str], *, timeout: int) -> None:
    logger.debug("Executing command: %s", " ".join(cmd))
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Command timed out after %ss: %s", timeout, " ".join(cmd))
        raise RuntimeError(f"Command timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        # Check if shutdown was requested (Ctrl+C or SIGTERM)
        if _shutdown_event.is_set():
            raise InterruptedError("Shutdown requested") from exc
        stderr = (exc.stderr or "").strip()
        if stderr:
            logger.error("Command failed (rc=%s): %s | stderr: %s", exc.returncode, " ".join(cmd), stderr)
        else:
            logger.error("Command failed (rc=%s): %s", exc.returncode, " ".join(cmd))
        raise RuntimeError(f"Command failed: {exc.returncode}") from exc


def _run_cmd_capture(cmd: list[str], *, timeout: int) -> str:
    logger.debug("Executing command (capture): %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Command timed out after %ss: %s", timeout, " ".join(cmd))
        raise RuntimeError(f"Command timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if stderr:
            logger.error("Command failed (rc=%s): %s | stderr: %s", exc.returncode, " ".join(cmd), stderr)
        else:
            logger.error("Command failed (rc=%s): %s", exc.returncode, " ".join(cmd))
        raise RuntimeError(f"Command failed: {exc.returncode}") from exc
    return result.stdout


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
        title_raw = _run_cmd_capture(["yt-dlp", "--no-playlist", "--get-title", url], timeout=120).strip()
    except Exception as exc:
        logger.warning("Could not fetch video title, using fallback filename: %s", exc)
        title_raw = "video"
    title = _sanitize_filename(title_raw)
    return f"{title} ({_quality_label(quality)})"


def _download_audio(job_id: str, url: str) -> Path:
    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    stem = _build_output_stem(url, "best")
    out = job_dir / f"{stem}.mp3"
    cmd = [
        "yt-dlp",
        "-f",
        "ba/b",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        str(job_dir / f"{stem}.%(ext)s"),
        url,
    ]
    _run_cmd(cmd, timeout=_TIMEOUT_DOWNLOAD)
    
    # Rename thumbnail to consistent name
    for thumb in job_dir.glob("*.jpg"):
        if thumb.name != "thumbnail.jpg":
            thumb.rename(job_dir / "thumbnail.jpg")
            break
    
    return out


def _download_best(job_id: str, url: str) -> Path:
    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    stem = _build_output_stem(url, "max")
    out = job_dir / f"{stem}.mp4"
    cmd = [
        "yt-dlp",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        str(job_dir / f"{stem}.%(ext)s"),
        url,
    ]
    _run_cmd(cmd, timeout=_TIMEOUT_DOWNLOAD)
    
    # Rename thumbnail to consistent name
    for thumb in job_dir.glob("*.jpg"):
        if thumb.name != "thumbnail.jpg":
            thumb.rename(job_dir / "thumbnail.jpg")
            break
    
    return out


def _download_and_transcode(job_id: str, url: str, quality: str) -> Path:
    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    stem = _build_output_stem(url, quality)
    temp = job_dir / f"{stem}.source.mp4"
    out = job_dir / f"{stem}.mp4"

    _emit(job_id, "downloading", "Downloading source for transcoding")
    _run_cmd(
        [
            "yt-dlp",
            "-f",
            "bv*[height<=720]+ba/b",
            "--merge-output-format",
            "mp4",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "-o",
            str(job_dir / f"{stem}.source.%(ext)s"),
            url,
        ],
        timeout=_TIMEOUT_DOWNLOAD,
    )
    
    # Rename thumbnail to consistent name
    for thumb in job_dir.glob("*.jpg"):
        if thumb.name != "thumbnail.jpg":
            thumb.rename(job_dir / "thumbnail.jpg")
            break

    scale = "scale=-2:720" if quality == "medium" else "scale=-2:480"
    _emit(job_id, "transcoding", f"Transcoding to {quality}")
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
    )

    try:
        temp.unlink(missing_ok=True)
    except Exception as exc:
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
    job_id, url, type_, quality = job

    try:
        if type_ == "audio":
            _emit(job_id, "downloading", "Downloading audio stream")
            out_path = _download_audio(job_id, url)
        elif quality == "max":
            _emit(job_id, "downloading", "Downloading best video+audio")
            out_path = _download_best(job_id, url)
        else:
            out_path = _download_and_transcode(job_id, url, quality)

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

    except InterruptedError:
        # Graceful shutdown - don't mark as error, just abort silently
        logger.info("Job %s interrupted by shutdown", job_id)
        update_job(job_id, status="queued")  # Requeue for next startup
        raise
    except Exception as exc:
        err_msg = f"Job failed: {exc}"
        logger.exception("Job %s failed", job_id)
        update_job(job_id, status="error", finished_at=_now_iso(), message=err_msg)
        _emit(job_id, "error", err_msg)


def worker() -> None:
    while True:
        if _shutdown_event.is_set() and job_queue.empty():
            return

        try:
            job = job_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        job_id = job[0]
        try:
            update_job(job_id, status="processing")
            _emit(job_id, "processing", "Worker picked up job")
            process_job(job)
        except InterruptedError:
            # Graceful shutdown - exit worker loop
            logger.debug("Worker exiting due to shutdown")
            job_queue.task_done()
            return
        except Exception:
            logger.exception("Unhandled worker error for job %s", job_id)
            try:
                update_job(job_id, status="error", finished_at=_now_iso())
                _emit(job_id, "error", "Internal worker error")
            except Exception:
                pass
        finally:
            try:
                job_queue.task_done()
            except ValueError:
                pass  # Already called task_done


def start_workers(n: int = 2) -> None:
    global _workers_started
    if _workers_started:
        return

    _shutdown_event.clear()
    for _ in range(n):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        _worker_threads.append(t)

    _workers_started = True


def stop_workers(timeout: float = 5.0) -> None:
    global _workers_started

    _shutdown_event.set()
    for t in list(_worker_threads):
        t.join(timeout=timeout)
    _worker_threads.clear()
    _workers_started = False
