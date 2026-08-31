#!/usr/bin/env python3
#
# app/routes/media.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Media routes for downloads, thumbnails, and job pages.

Single-identity application: there is exactly one credential (``_DEFAULT_USER``
plus the ``admin_password_hash`` setting in app/routes/auth.py) and the ``jobs``
table has no owner column. Every job therefore belongs to the only account that
can authenticate, and ``current_user(request)`` is the complete authorization
check for these routes - there is no second principal to isolate a job from.
Introducing additional accounts would require an owner column plus per-job
filtering here before that stays true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from ..db import DOWNLOADABLE_STATUSES, get_job
from ..utils.fs import AUDIO_SOURCE_EXTENSIONS, path_is_file
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
# Download table. ".webm" deliberately overrides the audio entry above: audio
# jobs are always stored as "<stem>.source.<ext>" and ".webm" is an internal
# source extension, so an audio .webm never reaches this table - it is served
# as MP3 by download() or via _AUDIO_MIME_TYPES by audio_source(). A .webm
# that does reach here came from the video merge fallback.
_KNOWN_MEDIA_TYPES = {
    **_AUDIO_MIME_TYPES,
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}
_NOSNIFF_HEADER = {"X-Content-Type-Options": "nosniff"}


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


async def _mp3_cache_is_valid(cache_path: Path, source_path: Path) -> bool:
    """Return True when the cached MP3 is at least as new as its source.

    The transcode is deterministic, so a cache that postdates its source stays
    valid for the life of the job; job retention removes both together. An
    absolute age limit would only re-run ffmpeg for an identical result.
    """
    if not await path_is_file(cache_path):
        return False

    try:
        cache_mtime = (await asyncio.to_thread(cache_path.stat)).st_mtime
        source_mtime = (await asyncio.to_thread(source_path.stat)).st_mtime
    except OSError:
        return False

    if cache_mtime >= source_mtime:
        return True

    logger.info("Removing outdated MP3 cache: %s", cache_path.name)
    await asyncio.to_thread(cache_path.unlink, missing_ok=True)
    return False


async def _get_ready_job(job_id: uuid.UUID) -> dict[str, object]:
    job = await asyncio.to_thread(get_job, str(job_id))
    if not job or job["status"] not in DOWNLOADABLE_STATUSES:
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


def is_internal_audio_source(file_path: Path) -> bool:
    """Return whether the file uses the internal ``<stem>.source.<ext>`` format.

    The marker describes storage provenance, not codec quality. Some supported
    source codecs are lossy even though they are kept without a second encode.
    """
    return file_path.stem.endswith(".source") and file_path.suffix.lower() in AUDIO_SOURCE_EXTENSIONS


def get_mp3_cache_path(source_path: Path) -> Path:
    """Get the cached MP3 path for an internal audio source file.

    Example: 'track.source.opus' -> 'track.mp3'
    """
    stem = source_path.stem.removesuffix(".source")
    return source_path.parent / f"{stem}.mp3"


def needs_browser_audio_fallback(file_path: Path) -> bool:
    """Return True when the source container/codec is not broadly browser-safe.

    iOS Safari is stricter than Chromium and will reject common containers like
    WebM/Opus or Ogg even though desktop Chrome can play them.

    The extension alone decides this because the inputs are not arbitrary: every
    audio file here comes from yt-dlp's ``-f ba/b -x`` path (app/worker.py), so
    the container implies the codec (.m4a -> AAC, .webm -> Opus/Vorbis). A
    ffprobe call per request would cost more than the occasional needless
    transcode it would avoid. Widen this to a codec check if audio ever enters
    from an uncontrolled source.
    """
    return file_path.suffix.lower() not in _BROWSER_SAFE_AUDIO_EXTENSIONS


async def _stop_transcode_process(proc: asyncio.subprocess.Process) -> None:
    """Stop and reap a still-running ffmpeg process."""
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    with suppress(ProcessLookupError):
        await asyncio.shield(proc.wait())


async def transcode_to_mp3(source_path: Path, output_path: Path) -> Path:
    """Transcode an internal audio source to high-quality MP3.

    Uses ffmpeg with -q:a 0 (VBR ~320kbps) for best quality.
    The transcoded file is cached for subsequent downloads.

    Args:
        source_path: Path to internal source audio
        output_path: Path where MP3 will be written

    Returns:
        Path to the MP3 file

    Raises:
        RuntimeError: If ffmpeg is missing, fails, or times out.
        OSError: If the temporary file cannot be written, replaced, or removed.
    """
    if await _mp3_cache_is_valid(output_path, source_path):
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
            if await _mp3_cache_is_valid(output_path, source_path):
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
            try:
                async with governor.transcode_semaphore:
                    if await _mp3_cache_is_valid(output_path, source_path):
                        return output_path

                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    except FileNotFoundError as exc:
                        raise RuntimeError("ffmpeg not found") from exc

                    try:
                        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
                    except BaseException:
                        # BaseException, not Exception: CancelledError (client
                        # disconnect, server shutdown) is the case that would
                        # otherwise leave ffmpeg running past its request.
                        await _stop_transcode_process(proc)
                        raise

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
    if await _mp3_cache_is_valid(mp3_path, file_path):
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
            status_code=404,
        )

    return templates.TemplateResponse(
        request=request,
        name="job.html",
        context={"job": job_to_dict(job), "csrf_token": csrf_token},
    )


async def build_job_file_response(job_id: uuid.UUID) -> Response:
    """Resolve a job's output file and return it as a download response.

    Shared by the authenticated /download route and the public share-link
    route so both deliver byte-identical results (including the on-the-fly
    MP3 transcode for internal audio sources). Raises no exceptions: a missing
    or not-yet-ready job comes back as the matching JSON error response.
    """
    try:
        job, file_path = await _get_ready_job_file(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    if job["type"] == "audio" and is_internal_audio_source(file_path):
        try:
            mp3_path = await _ensure_mp3(file_path)
        except (RuntimeError, OSError) as exc:
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


@router.get("/download/{job_id}")
@limiter.limit("30/minute")
async def download(request: Request, job_id: uuid.UUID):
    """Download a job's output file."""
    if not current_user(request):
        return RedirectResponse(url="/login", status_code=303)

    return await build_job_file_response(job_id)


@router.get("/audio-source/{job_id}")
@limiter.limit("60/minute")
async def audio_source(request: Request, job_id: uuid.UUID):
    """Serve the job's stored audio file for trimming.

    The X-Audio-Quality header indicates whether the served file is the
    original source or a cached MP3 fallback for browser playback. The
    response is served inline: it feeds the waveform/trim UI in the page,
    unlike /download/{job_id}, which is an attachment.
    """
    if not current_user(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    try:
        job, file_path = await _get_ready_job_file(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    if job["type"] != "audio":
        return JSONResponse(status_code=400, content={"error": "Audio source is only available for audio jobs"})

    quality = "lossless" if is_internal_audio_source(file_path) else "lossy"

    if needs_browser_audio_fallback(file_path):
        try:
            mp3_path = await _ensure_mp3(file_path)
        except (RuntimeError, OSError) as exc:
            logger.error("Failed to transcode audio source for job %s: %s", job_id, exc)
            return JSONResponse(status_code=500, content={"error": "Audio transcoding failed"})

        return FileResponse(
            path=mp3_path,
            filename=mp3_path.name,
            media_type="audio/mpeg",
            content_disposition_type="inline",
            headers={**_NOSNIFF_HEADER, "X-Audio-Quality": f"{quality}-mp3-fallback"},
        )

    media_type = _AUDIO_MIME_TYPES.get(file_path.suffix.lower(), "application/octet-stream")

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type,
        content_disposition_type="inline",
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
