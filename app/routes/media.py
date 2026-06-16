#!/usr/bin/env python3
#
# app/routes/media.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Media routes for downloads, thumbnails, and job pages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import mimetypes
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from ..common.rate_limit import limiter
from ..db import COMPLETED_STATUSES, get_job
from ..utils.fs import LOSSLESS_AUDIO_SOURCE_EXTENSIONS, path_is_file
from ..governor import governor
from .api import job_to_dict
from .auth import current_user, require_html_auth

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

# Constants
_BROWSER_SAFE_AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aac", ".wav"})
_AUDIO_MIME_TYPES = {
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
}
_KNOWN_MEDIA_TYPES = {
    **_AUDIO_MIME_TYPES,
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}
_NOSNIFF_HEADER = {"X-Content-Type-Options": "nosniff"}
MAX_MP3_CACHE_AGE_SECONDS = 7 * 24 * 3600


@dataclass(slots=True, frozen=True)
class MediaContext:
    data_dir: Path
    base_dir: Path
    templates: "Jinja2Templates"

# Module-level state
_MEDIA_CONTEXT: MediaContext | None = None
_transcode_locks: dict[Path, asyncio.Lock] = {}
_transcode_lock_refs: dict[Path, int] = {}
_transcode_locks_guard = threading.Lock()


def init_media(
    data_dir: Path,
    base_dir: Path,
    templates: "Jinja2Templates",
) -> None:
    """Initialize the media module with required dependencies."""
    global _MEDIA_CONTEXT
    context = MediaContext(data_dir=data_dir, base_dir=base_dir, templates=templates)
    if _MEDIA_CONTEXT is not None and _MEDIA_CONTEXT != context:
        raise RuntimeError("Media module already initialized with a different context")
    _MEDIA_CONTEXT = context


def _require_media_context() -> MediaContext:
    if _MEDIA_CONTEXT is None:
        raise RuntimeError("Media module not initialized: call init_media() first")
    return _MEDIA_CONTEXT


def _require_data_dir() -> Path:
    return _require_media_context().data_dir


def _require_base_dir() -> Path:
    return _require_media_context().base_dir


def _require_templates() -> "Jinja2Templates":
    return _require_media_context().templates




def _guess_media_type(file_path: Path) -> str:
    media_type = _KNOWN_MEDIA_TYPES.get(file_path.suffix.lower())
    if media_type:
        return media_type

    media_type, _ = mimetypes.guess_type(file_path.name)
    return media_type or "application/octet-stream"


async def _has_fresh_mp3_cache(cache_path: Path) -> bool:
    if not await path_is_file(cache_path):
        return False

    try:
        cache_stat = await asyncio.to_thread(cache_path.stat)
    except OSError:
        return False

    cache_age_seconds = datetime.now(UTC).timestamp() - cache_stat.st_mtime
    if cache_age_seconds <= MAX_MP3_CACHE_AGE_SECONDS:
        return True

    logger.info("Removing stale MP3 cache: %s", cache_path.name)
    await asyncio.to_thread(cache_path.unlink, missing_ok=True)
    return False


async def _get_ready_job(job_id: uuid.UUID) -> dict[str, object]:
    job = await asyncio.to_thread(get_job, str(job_id))
    if not job or job["status"] not in COMPLETED_STATUSES:
        raise HTTPException(status_code=404, detail="not ready")
    return job


async def _get_ready_job_file(job_id: uuid.UUID) -> tuple[dict[str, object], Path]:
    job = await _get_ready_job(job_id)
    raw_filename = job["filename"]
    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")

    file_path = resolve_job_path(raw_filename)
    if not await path_is_file(file_path):
        raise HTTPException(status_code=404, detail="not found")
    return job, file_path


async def _get_thumbnail_path(job_id: uuid.UUID) -> Path:
    data_dir = _require_data_dir()
    thumb_path = data_dir / str(job_id) / "thumbnail.jpg"
    if not await path_is_file(thumb_path):
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
    data_dir = _require_data_dir().resolve()

    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")

    file_path = Path(str(raw_filename).strip())
    if not file_path.is_absolute():
        file_path = data_dir / file_path
    try:
        file_path = file_path.resolve()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    try:
        file_path.relative_to(data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    return file_path


def is_lossless_audio_source(file_path: Path) -> bool:
    """Check if the file is a lossless audio source (internal format).

    Lossless source files are stored as '<stem>.source.<ext>'.
    """
    return file_path.stem.endswith(".source") and file_path.suffix.lower() in LOSSLESS_AUDIO_SOURCE_EXTENSIONS


def get_mp3_cache_path(source_path: Path) -> Path:
    """Get the cached MP3 path for a lossless audio source file.

    Example: 'track.source.opus' -> 'track.mp3'
    """
    stem = source_path.stem.replace(".source", "")
    return source_path.parent / f"{stem}.mp3"


def needs_browser_audio_fallback(file_path: Path) -> bool:
    """Return True when the source container/codec is not broadly browser-safe.

    iOS Safari is stricter than Chromium and will reject common containers like
    WebM/Opus or Ogg even though desktop Chrome can play them.
    """
    return file_path.suffix.lower() not in _BROWSER_SAFE_AUDIO_EXTENSIONS


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
    if await _has_fresh_mp3_cache(output_path):
        return output_path

    with _transcode_locks_guard:
        lock = _transcode_locks.get(output_path)
        if lock is None:
            lock = asyncio.Lock()
            _transcode_locks[output_path] = lock
            _transcode_lock_refs[output_path] = 0
        _transcode_lock_refs[output_path] = _transcode_lock_refs.get(output_path, 0) + 1

    try:
        async with lock:
            if await _has_fresh_mp3_cache(output_path):
                return output_path

            temp_path = output_path.with_name(f"{output_path.name}.tmp.{uuid.uuid4().hex[:8]}")
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
            proc: asyncio.subprocess.Process | None = None

            try:
                async with governor.transcode_semaphore:
                    if await path_is_file(output_path):
                        return output_path

                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    except FileNotFoundError as exc:
                        raise RuntimeError("ffmpeg not found") from exc

                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

                    if proc.returncode != 0:
                        logger.error(
                            "FFmpeg transcode failed: %s",
                            stderr.decode(errors="replace") if stderr else "unknown error",
                        )
                        raise RuntimeError("Audio transcoding failed")

                    await asyncio.to_thread(temp_path.replace, output_path)
                logger.info("Transcoded audio to MP3: %s", output_path.name)
                return output_path

            except asyncio.TimeoutError as exc:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    with suppress(ProcessLookupError):
                        await proc.wait()
                raise RuntimeError("Audio transcoding timed out") from exc
            finally:
                if await path_is_file(temp_path):
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
    finally:
        with _transcode_locks_guard:
            next_ref_count = _transcode_lock_refs.get(output_path, 0) - 1
            if next_ref_count <= 0:
                _transcode_lock_refs.pop(output_path, None)
                _transcode_locks.pop(output_path, None)
            else:
                _transcode_lock_refs[output_path] = next_ref_count


async def _ensure_mp3(file_path: Path) -> Path:
    """Return an MP3 path, transcoding the source when the cache is missing."""
    mp3_path = get_mp3_cache_path(file_path)
    if await _has_fresh_mp3_cache(mp3_path):
        return mp3_path

    await transcode_to_mp3(file_path, mp3_path)
    return mp3_path


# ============================================================================
# Routes
# ============================================================================


@router.get("/job/{job_id}", response_class=HTMLResponse)
@limiter.limit("120/minute")
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
        context={"job": job_to_dict(job), "csrf_token": csrf_token},
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
        try:
            mp3_path = await _ensure_mp3(file_path)
        except RuntimeError as exc:
            logger.error("Failed to transcode audio for job %s: %s", job_id, exc)
            return JSONResponse(status_code=500, content={"error": "Audio transcoding failed"})

        return FileResponse(
            path=mp3_path,
            filename=mp3_path.name,
            media_type="audio/mpeg",
            headers=_NOSNIFF_HEADER,
        )

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=_guess_media_type(file_path),
        headers=_NOSNIFF_HEADER,
    )


@router.get("/audio-source/{job_id}")
@limiter.limit("60/minute")
async def audio_source(request: Request, job_id: uuid.UUID):
    """Serve the job's stored audio file for trimming.

    The X-Audio-Quality header indicates whether the served file is the
    original source or a cached MP3 fallback for browser playback.
    """
    if not current_user(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    try:
        _, file_path = await _get_ready_job_file(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    quality = "lossless" if is_lossless_audio_source(file_path) else "lossy"

    if needs_browser_audio_fallback(file_path):
        try:
            mp3_path = await _ensure_mp3(file_path)
        except RuntimeError as exc:
            logger.error("Failed to transcode audio source for job %s: %s", job_id, exc)
            return JSONResponse(status_code=500, content={"error": "Audio transcoding failed"})

        return FileResponse(
            path=mp3_path,
            filename=mp3_path.name,
            media_type="audio/mpeg",
            headers={**_NOSNIFF_HEADER, "X-Audio-Quality": f"{quality}-mp3-fallback"},
        )

    media_type = _AUDIO_MIME_TYPES.get(file_path.suffix.lower(), "application/octet-stream")

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type,
        headers={**_NOSNIFF_HEADER, "X-Audio-Quality": quality},
    )


@router.get("/favicon.ico")
async def favicon():
    """Serve favicon from static/img if exists, else 204."""
    ico_path = _require_base_dir() / "static" / "img" / "favicon.ico"
    if await path_is_file(ico_path):
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

    return FileResponse(path=thumb_path, media_type="image/jpeg", headers=_NOSNIFF_HEADER)
