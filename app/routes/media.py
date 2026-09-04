#!/usr/bin/env python3
#
# app/routes/media.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Media routes for downloads, thumbnails, and job pages.

Single-identity application: one credential, and ``jobs`` has no owner column,
so every job belongs to the only account that can authenticate and
``current_user(request)`` is the complete authorization check here. More
accounts would need an owner column plus per-job filtering.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from ..bpm_naming import tagged_download_name
from ..common.rate_limit import limiter
from ..db import DOWNLOADABLE_STATUSES, get_job, get_settings
from ..governor import governor
from ..utils.fs import AUDIO_SOURCE_EXTENSIONS, path_is_file, resolve_within_root
from .api import job_to_dict
from .auth import get_csrf_token, require_html_auth, require_user_json

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

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
# ".webm" overrides the audio entry above on purpose: an audio .webm is always
# stored as "<stem>.source.webm" and never reaches this table, so a .webm here
# is video (from the merge fallback).
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
    templates: Jinja2Templates


_MEDIA_CONTEXT: MediaContext | None = None
_transcode_locks: dict[Path, asyncio.Lock] = {}
_transcode_lock_refs: dict[Path, int] = {}
_transcode_locks_guard = threading.Lock()


def init_media(
    data_dir: Path,
    base_dir: Path,
    templates: Jinja2Templates,
) -> None:
    global _MEDIA_CONTEXT
    context = MediaContext(data_dir=data_dir, base_dir=base_dir, templates=templates)
    if _MEDIA_CONTEXT is not None and context != _MEDIA_CONTEXT:
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


def _require_templates() -> Jinja2Templates:
    return _require_media_context().templates


def _guess_media_type(file_path: Path) -> str:
    media_type = _KNOWN_MEDIA_TYPES.get(file_path.suffix.lower())
    if media_type:
        return media_type

    media_type, _ = mimetypes.guess_type(file_path.name)
    return media_type or "application/octet-stream"


async def _mp3_cache_is_valid(cache_path: Path, source_path: Path) -> bool:
    """Return True when the cached MP3 is at least as new as its source.

    The transcode is deterministic, so an mtime check is enough; retention
    removes both together.
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
    """Resolve a job's raw filename to a Path inside DATA_DIR.

    Raises HTTPException when the path is missing, unresolvable, or escapes
    DATA_DIR.
    """
    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")

    file_path = Path(str(raw_filename).strip())
    try:
        return resolve_within_root(file_path, _require_data_dir())
    except OSError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc


def is_internal_audio_source(file_path: Path) -> bool:
    """Whether the file uses the internal ``<stem>.source.<ext>`` format.

    Describes storage provenance, not codec quality (some kept-as-is codecs
    are still lossy).
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

    iOS Safari rejects WebM/Opus and Ogg that desktop Chrome plays. The
    extension is enough here because every audio file comes from yt-dlp's
    ``-f ba/b -x`` path, so the container implies the codec. Widen to a codec
    check if audio ever enters from an uncontrolled source.
    """
    return file_path.suffix.lower() not in _BROWSER_SAFE_AUDIO_EXTENSIONS


async def stop_ffmpeg_process(proc: asyncio.subprocess.Process) -> None:
    """Stop and reap a still-running ffmpeg process."""
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    with suppress(ProcessLookupError):
        await asyncio.shield(proc.wait())


async def transcode_to_mp3(source_path: Path, output_path: Path) -> Path:
    """Transcode an internal audio source to MP3 (ffmpeg -q:a 0, ~320kbps VBR)
    and cache it at ``output_path``.

    Raises RuntimeError (ffmpeg missing/failed/timed out) or OSError.
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
                        # BaseException catches CancelledError (client
                        # disconnect / shutdown), which would else orphan ffmpeg.
                        await stop_ffmpeg_process(proc)
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

            except TimeoutError as exc:
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


@router.get("/job/{job_id}", response_class=HTMLResponse)
@limiter.limit("120/minute")
async def job_page(request: Request, job_id: uuid.UUID):
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    templates = _require_templates()

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    settings = await asyncio.to_thread(get_settings)
    auth_enabled = bool(settings.get("enable_authentication", False))
    csrf_token = get_csrf_token(request)
    if not job:
        return templates.TemplateResponse(
            request=request,
            name="job.html",
            context={"job": None, "job_id": job_id_str, "auth_enabled": auth_enabled, "csrf_token": csrf_token},
            status_code=404,
        )

    return templates.TemplateResponse(
        request=request,
        name="job.html",
        context={"job": job_to_dict(job), "auth_enabled": auth_enabled, "csrf_token": csrf_token},
    )


async def build_job_file_response(job_id: uuid.UUID) -> Response:
    """Resolve a job's output file into a download response.

    Shared by /download and the share-link route so both are byte-identical
    (including the on-the-fly MP3 transcode). Never raises: a missing or
    not-ready job comes back as a JSON error response.
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
            filename=tagged_download_name(mp3_path, job["bpm"]),
            media_type="audio/mpeg",
            headers=_NOSNIFF_HEADER,
        )

    return FileResponse(
        path=file_path,
        filename=tagged_download_name(file_path, job["bpm"]),
        media_type=_guess_media_type(file_path),
        headers=_NOSNIFF_HEADER,
    )


@router.get("/download/{job_id}")
@limiter.limit("30/minute")
async def download(request: Request, job_id: uuid.UUID):
    redirect = require_html_auth(request)
    if redirect:
        return redirect

    return await build_job_file_response(job_id)


@router.get("/audio-source/{job_id}")
@limiter.limit("60/minute")
async def audio_source(request: Request, job_id: uuid.UUID, _user: str = Depends(require_user_json)):
    """Serve a job's stored audio inline for the waveform/trim UI.

    ``X-Audio-Quality`` says whether it is the original source or a cached MP3
    fallback. Inline, unlike the /download attachment.
    """
    _ = request  # required by @limiter.limit, unused now that auth is a Depends()
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
async def thumbnail(request: Request, job_id: uuid.UUID, _user: str = Depends(require_user_json)):
    _ = request  # required by @limiter.limit, unused now that auth is a Depends()
    try:
        thumb_path = await _get_thumbnail_path(job_id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return FileResponse(path=thumb_path, media_type="image/jpeg", headers=_NOSNIFF_HEADER)
