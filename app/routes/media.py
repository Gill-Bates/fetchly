#!/usr/bin/env python3
#
# app/routes/media.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Media routes for downloads, thumbnails, and job pages."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from slowapi import Limiter

from ..common.rate_limit import limiter
from ..db import get_job
from ..governor import governor
from .auth import current_user, require_html_auth

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

# Constants
_LOSSLESS_AUDIO_EXTENSIONS = frozenset({".opus", ".m4a", ".webm", ".ogg", ".aac", ".flac", ".wav"})

# Module-level state
_DATA_DIR: Path | None = None
_BASE_DIR: Path | None = None
_templates: "Jinja2Templates | None" = None
_limiter: Limiter | None = None


def init_media(
    data_dir: Path,
    base_dir: Path,
    templates: "Jinja2Templates",
    limiter: Limiter,
) -> None:
    """Initialize the media module with required dependencies."""
    global _DATA_DIR, _BASE_DIR, _templates, _limiter
    _DATA_DIR = data_dir
    _BASE_DIR = base_dir
    _templates = templates
    _limiter = limiter


def _require_data_dir() -> Path:
    if _DATA_DIR is None:
        raise RuntimeError("Media module not initialized: call init_media() first")
    return _DATA_DIR


def _require_base_dir() -> Path:
    if _BASE_DIR is None:
        raise RuntimeError("Media module not initialized: call init_media() first")
    return _BASE_DIR


def _require_templates() -> "Jinja2Templates":
    if _templates is None:
        raise RuntimeError("Media module not initialized: call init_media() first")
    return _templates


async def _path_is_file(path: Path) -> bool:
    return await asyncio.to_thread(path.is_file)


def _guess_media_type(file_path: Path) -> str:
    media_type, _ = mimetypes.guess_type(file_path.name)
    return media_type or "application/octet-stream"


async def _get_ready_job(job_id: uuid.UUID) -> dict[str, object]:
    job = await asyncio.to_thread(get_job, str(job_id))
    if not job or job["status"] not in {"done", "analysis", "analysis_done"}:
        raise HTTPException(status_code=404, detail="not ready")
    return job


async def _get_ready_job_file(job_id: uuid.UUID) -> tuple[dict[str, object], Path]:
    job = await _get_ready_job(job_id)
    raw_filename = job["filename"]
    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")

    file_path = resolve_job_path(raw_filename)
    if not await _path_is_file(file_path):
        raise HTTPException(status_code=404, detail="not found")
    return job, file_path


async def _get_thumbnail_path(job_id: uuid.UUID) -> Path:
    data_dir = _require_data_dir()
    thumb_path = data_dir / str(job_id) / "thumbnail.jpg"
    if not await _path_is_file(thumb_path):
        raise HTTPException(status_code=404, detail="not found")
    return thumb_path


def resolve_job_path(raw_filename: str | None) -> Path:
    """Resolve and validate a job file path.

    Args:
        raw_filename: The raw filename from the job record

    Returns:
        Resolved Path within DATA_DIR

    Raises:
        HTTPException: If path is invalid or outside DATA_DIR
    """
    data_dir = _require_data_dir()

    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")
    file_path = Path(str(raw_filename)).resolve()
    try:
        file_path.relative_to(data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    return file_path


def is_lossless_audio_source(file_path: Path) -> bool:
    """Check if the file is a lossless audio source (internal format).

    Lossless source files are stored with '.source.' in the filename.
    """
    return ".source." in file_path.name and file_path.suffix.lower() in _LOSSLESS_AUDIO_EXTENSIONS


def get_mp3_cache_path(source_path: Path) -> Path:
    """Get the cached MP3 path for a lossless audio source file.

    Example: 'track.source.opus' -> 'track.mp3'
    """
    stem = source_path.stem.replace(".source", "")
    return source_path.parent / f"{stem}.mp3"


async def transcode_to_mp3(source_path: Path, output_path: Path) -> Path:
    """Transcode a lossless audio file to high-quality MP3.

    Uses ffmpeg with -q:a 0 (VBR ~320kbps) for best quality.
    The transcoded file is cached for subsequent downloads.

    Args:
        source_path: Path to lossless source audio
        output_path: Path where MP3 will be written

    Returns:
        Path to the MP3 file

    Raises:
        RuntimeError: If ffmpeg transcoding fails
    """
    if await _path_is_file(output_path):
        return output_path

    temp_path = output_path.with_suffix(".mp3.tmp")

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source_path),
        "-vn",
        "-codec:a", "libmp3lame",
        "-q:a", "0",
        "-map_metadata", "0",
        "-f", "mp3",
        str(temp_path),
    ]

    try:
        async with governor.transcode_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

            if proc.returncode != 0:
                logger.error("FFmpeg transcode failed: %s", stderr.decode() if stderr else "unknown error")
                raise RuntimeError("Audio transcoding failed")

        await asyncio.to_thread(temp_path.rename, output_path)
        logger.info("Transcoded audio to MP3: %s", output_path.name)
        return output_path

    except asyncio.TimeoutError:
        if await _path_is_file(temp_path):
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
        raise RuntimeError("Audio transcoding timed out")
    except Exception:
        if await _path_is_file(temp_path):
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
        raise


# ============================================================================
# Routes
# ============================================================================


@router.get("/job/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: uuid.UUID):
    """Job detail page."""
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    templates = _require_templates()

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    csrf_token = getattr(request.state, "csrf_token", "")
    if not job:
        return templates.TemplateResponse(
            request=request,
            name="job.html",
            context={"job": None, "job_id": job_id_str, "csrf_token": csrf_token},
        )

    return templates.TemplateResponse(
        request=request,
        name="job.html",
        context={"job": job, "csrf_token": csrf_token},
    )


@router.get("/download/{job_id}")
@limiter.limit("30/minute")
async def download(request: Request, job_id: uuid.UUID):
    """Download a job's output file."""
    if not current_user(request):
        return RedirectResponse(url="/login", status_code=303)

    try:
        job, file_path = await _get_ready_job_file(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    if job["type"] == "audio" and is_lossless_audio_source(file_path):
        mp3_path = get_mp3_cache_path(file_path)

        if not await _path_is_file(mp3_path):
            try:
                await transcode_to_mp3(file_path, mp3_path)
            except RuntimeError as exc:
                logger.error("Failed to transcode audio for job %s: %s", job_id, exc)
                return JSONResponse(status_code=500, content={"error": "Audio transcoding failed"})

        return FileResponse(path=mp3_path, filename=mp3_path.name, media_type="audio/mpeg")

    return FileResponse(path=file_path, filename=file_path.name, media_type=_guess_media_type(file_path))


@router.get("/audio-source/{job_id}")
@limiter.limit("60/minute")
async def audio_source(request: Request, job_id: uuid.UUID):
    """Serve audio source for trimming.

    Prefers lossless source file (e.g., .source.opus) but falls back
    to MP3 if no lossless source exists (for older jobs or direct
    MP3 downloads). The X-Audio-Quality header indicates which:
    - "lossless": Original source file served
    - "lossy": MP3 fallback used
    """
    if not current_user(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    try:
        _, file_path = await _get_ready_job_file(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    quality = "lossless" if is_lossless_audio_source(file_path) else "lossy"

    mime_types = {
        ".opus": "audio/opus",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        ".aac": "audio/aac",
        ".mp3": "audio/mpeg",
    }
    media_type = mime_types.get(file_path.suffix.lower(), "application/octet-stream")

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type,
        headers={"X-Audio-Quality": quality},
    )


@router.get("/favicon.ico")
async def favicon():
    """Serve favicon from static/img if exists, else 204."""
    ico_path = _require_base_dir() / "static" / "img" / "favicon.ico"
    if await _path_is_file(ico_path):
        return FileResponse(path=ico_path, media_type="image/x-icon")
    return Response(status_code=204)


@router.get("/thumbnail/{job_id}")
@limiter.limit("120/minute")
async def thumbnail(request: Request, job_id: uuid.UUID):
    """Serve cached thumbnail for a job."""
    if not current_user(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    try:
        thumb_path = await _get_thumbnail_path(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return FileResponse(path=thumb_path, media_type="image/jpeg")
