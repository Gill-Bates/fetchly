#!/usr/bin/env python3
#
# app/routes/trim.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Audio trimming routes for high-quality segment extraction."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..common.rate_limit import limiter
from ..db import get_job
from ..governor import governor
from .auth import require_user_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trim", tags=["trim"])

# Module-level state
_DATA_DIR: Path | None = None
_resolve_job_path: Callable[[str | None], Path] | None = None
_TRIM_ID_RE = re.compile(r"^\d+_\d+$")


class TrimRequest(BaseModel):
    start: float = Field(ge=0, description="Start time in seconds")
    end: float = Field(gt=0, description="End time in seconds")


class TrimResponse(BaseModel):
    ok: bool
    cached: bool
    trim_id: str
    duration: float
    file: str


def init_trim(
    data_dir: Path,
    resolve_job_path_func: Callable[[str | None], Path],
) -> None:
    """Initialize the trim module with required dependencies."""
    global _DATA_DIR, _resolve_job_path
    _DATA_DIR = data_dir
    _resolve_job_path = resolve_job_path_func


def _require_init() -> tuple[Path, Callable[[str | None], Path]]:
    if _DATA_DIR is None or _resolve_job_path is None:
        raise RuntimeError("Trim module not initialized: call init_trim() first")
    return _DATA_DIR, _resolve_job_path


async def _path_is_file(path: Path) -> bool:
    return await asyncio.to_thread(path.is_file)


async def _ensure_directory(path: Path) -> None:
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)


async def _replace_file(source: Path, target: Path) -> None:
    await asyncio.to_thread(source.replace, target)


async def _unlink_file(path: Path) -> None:
    await asyncio.to_thread(path.unlink, missing_ok=True)


def _validate_trim_id(trim_id: str) -> str:
    if not _TRIM_ID_RE.fullmatch(trim_id):
        raise HTTPException(status_code=400, detail={"error": "Invalid trim_id format"})
    return trim_id


async def _delete_trim_files(output_dir: Path, trim_id: str | None) -> int:
    def _delete() -> int:
        if trim_id is not None:
            trim_path = output_dir / f"trim_{trim_id}.wav"
            if trim_path.is_file():
                trim_path.unlink(missing_ok=True)
                return 1
            return 0

        deleted = 0
        for trim_path in output_dir.glob("trim_*.wav"):
            trim_path.unlink(missing_ok=True)
            deleted += 1
        return deleted

    return await asyncio.to_thread(_delete)


@asynccontextmanager
async def _acquire_trim_lock(lock_file: Path) -> AsyncIterator[None]:
    """Acquire an advisory trim lock without blocking the event loop."""

    def _lock() -> int:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(lock_fd)
            raise
        return lock_fd

    def _unlock(lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            with suppress(OSError):
                os.unlink(lock_file)

    lock_fd = await asyncio.to_thread(_lock)
    try:
        yield
    finally:
        await asyncio.to_thread(_unlock, lock_fd)


@router.post("/{job_id}", response_model=None)
@limiter.limit("10/minute")
async def trim_audio(request: Request, job_id: uuid.UUID, body: TrimRequest, _user: str = Depends(require_user_json)) -> TrimResponse | JSONResponse:
    """Trim an audio file to the requested start and end times."""
    _ = request
    data_dir, resolve_job_path = _require_init()

    start = body.start
    end = body.end
    
    # Validate selection
    if end <= start:
        return JSONResponse(status_code=400, content={"error": "End time must be after start time"})
    
    duration = end - start
    if duration < 1:
        return JSONResponse(status_code=400, content={"error": "Selection too short (minimum 1 second)"})
    
    if duration > 600:  # 10 minutes max
        return JSONResponse(status_code=400, content={"error": "Selection too long (maximum 10 minutes)"})
    
    # Get job
    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    
    if job["type"] != "audio":
        return JSONResponse(status_code=400, content={"error": "Trim only works with audio jobs"})
    
    if job["status"] not in ("done", "analysis", "analysis_done"):
        return JSONResponse(status_code=400, content={"error": "Job not ready"})
    
    # Validate end against known duration (if available)
    # sqlite3.Row doesn't have .get(), use bracket access with fallback
    job_duration = job["duration"] if "duration" in job else None
    if job_duration is not None and end > float(job_duration):
        return JSONResponse(status_code=400, content={"error": f"End time ({end:.1f}s) exceeds track duration ({job_duration:.1f}s)"})
    
    # Resolve source file
    raw_filename = job["filename"]
    try:
        source_path = resolve_job_path(raw_filename)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    
    if not await _path_is_file(source_path):
        return JSONResponse(status_code=404, content={"error": "Source file not found"})
    
    # Check if source is an audio file (lossless preferred, MP3 fallback)
    audio_extensions = {".opus", ".ogg", ".flac", ".wav", ".m4a", ".webm", ".aac", ".mp3"}
    if source_path.suffix.lower() not in audio_extensions:
        return JSONResponse(status_code=400, content={"error": "Not an audio file"})
    
    # Prepare output with unique trim_id based on timestamps
    output_dir = data_dir / job_id_str
    await _ensure_directory(output_dir)
    
    # Generate deterministic trim_id from start/end (millisecond precision)
    trim_id = f"{int(start * 1000)}_{int(end * 1000)}"
    
    # Lock to prevent parallel trims for the same requested segment.
    lock_file = output_dir / f".trim_{trim_id}.lock"
    
    # Use WAV output for broad Lalal compatibility.
    # This is a PCM re-encode, not bit-exact stream copy.
    out_file = output_dir / f"trim_{trim_id}.wav"
    temp_file = output_dir / f"trim_{uuid.uuid4().hex}.wav.tmp"
    
    # Place -ss after -i for more accurate trims.
    # Use -t to trim by exact duration and keep original channel layout.
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source_path),
        "-ss", str(start),
        "-t", str(duration),
        "-vn",  # No video
        "-acodec", "pcm_s16le",  # PCM 16-bit for WAV
        "-f", "wav",  # Explicit format (required for .tmp extension)
        str(temp_file),
    ]
    
    # Dynamic timeout with cap to avoid excessive wait times.
    timeout = min(300, max(30, int(duration * 2)))
    
    try:
        async with _acquire_trim_lock(lock_file):
            if await _path_is_file(out_file):
                logger.info("Trim cache hit: %s", out_file.name)
                return TrimResponse(
                    ok=True,
                    cached=True,
                    trim_id=trim_id,
                    duration=duration,
                    file=out_file.name,
                )

            async with governor.transcode_semaphore:
                proc: asyncio.subprocess.Process | None = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                stderr = b""
                try:
                    async with asyncio.timeout(timeout):
                        _, stderr = await proc.communicate()
                except TimeoutError:
                    await _unlink_file(temp_file)
                    return JSONResponse(status_code=500, content={"error": "Trim operation timed out"})
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        with suppress(ProcessLookupError):
                            await proc.wait()

                if proc.returncode != 0:
                    err_text = (stderr.decode(errors="replace") if stderr else "unknown error").strip()
                    logger.error("FFmpeg trim failed: %s", err_text)
                    await _unlink_file(temp_file)
                    return JSONResponse(status_code=500, content={"error": f"Trim failed: {err_text[:500]}"})

                # Atomic rename after ffmpeg finished successfully.
                await _replace_file(temp_file, out_file)
                logger.info("Trimmed audio: %s (%.2fs - %.2fs)", out_file.name, start, end)
                return TrimResponse(
                    ok=True,
                    cached=False,
                    trim_id=trim_id,
                    duration=duration,
                    file=out_file.name,
                )

    except BlockingIOError:
        await asyncio.sleep(0.5)
        if await _path_is_file(out_file):
            logger.info("Trim cache became available after lock contention: %s", out_file.name)
            return TrimResponse(
                ok=True,
                cached=True,
                trim_id=trim_id,
                duration=duration,
                file=out_file.name,
            )
        return JSONResponse(status_code=409, content={"error": "Trim already in progress"})

    except FileNotFoundError:
        await _unlink_file(temp_file)
        logger.error("FFmpeg binary not found while trimming job %s", job_id)
        return JSONResponse(status_code=500, content={"error": "FFmpeg is not installed on the server"})

    except OSError as exc:
        await _unlink_file(temp_file)
        logger.exception("Trim failed for job %s", job_id)
        return JSONResponse(status_code=500, content={"error": f"Trim failed: {exc}"})
    finally:
        await _unlink_file(temp_file)


@router.delete("/{job_id}", response_model=None)
@limiter.limit("30/minute")
async def delete_trim(request: Request, job_id: uuid.UUID, trim_id: str | None = None, _user: str = Depends(require_user_json)):
    """Delete trimmed audio file(s) for a job.
    
    Args:
        job_id: The job UUID
        trim_id: Optional specific trim ID to delete. If not provided, deletes all trim files.
    
    Useful for cleanup after Lalal processing or when user wants to re-trim.
    """
    _ = request
    data_dir, _ = _require_init()
    
    job_id_str = str(job_id)
    output_dir = data_dir / job_id_str
    
    deleted_count = 0
    
    if trim_id:
        # Delete specific trim
        deleted_count = await _delete_trim_files(output_dir, _validate_trim_id(trim_id))
        if deleted_count:
            logger.info("Deleted trim file: %s", output_dir / f"trim_{trim_id}.wav")
    else:
        # Delete all trim files
        deleted_count = await _delete_trim_files(output_dir, None)
        if deleted_count:
            logger.info("Deleted %d trim file(s) for job %s", deleted_count, job_id_str)
    
    return {"ok": True, "deleted": deleted_count}


@router.get("/{job_id}/{trim_id}/download", response_model=None)
@limiter.limit("30/minute")
async def download_trim(request: Request, job_id: uuid.UUID, trim_id: str, _user: str = Depends(require_user_json)):
    """Download a trimmed audio file.
    
    Args:
        job_id: The job UUID
        trim_id: The trim identifier (format: startMs_endMs)
    
    Returns:
        The trimmed WAV file as a download.
    """
    _ = request
    data_dir, _ = _require_init()
    
    trim_id = _validate_trim_id(trim_id)
    
    job_id_str = str(job_id)
    trim_path = data_dir / job_id_str / f"trim_{trim_id}.wav"
    
    if not await _path_is_file(trim_path):
        return JSONResponse(status_code=404, content={"error": "Trimmed file not found. Please trim again."})
    
    # Get job info for filename
    job = await asyncio.to_thread(get_job, job_id_str)
    video_title = job["video_title"] if job and "video_title" in job else None
    if video_title:
        # Sanitize filename
        safe_title = "".join(c for c in video_title if c.isalnum() or c in " -_").strip()[:100]
        filename = f"{safe_title}_trimmed.wav"
    else:
        filename = f"trimmed_{trim_id}.wav"
    
    return FileResponse(
        path=trim_path,
        filename=filename,
        media_type="audio/wav",
    )
