#!/usr/bin/env python3
#
# app/routes/lalal.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Lalal.ai integration routes."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import stat as stat_module
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import time
from typing import Annotated, Any, Callable, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from ..common.rate_limit import limiter
from ..db import DOWNLOADABLE_STATUSES, get_job, get_settings, set_settings, update_job
from ..governor import governor
from ..lalal_policy import stem_download_name
from ..utils.fs import TRIM_ID_RE, path_is_file
from ..utils.template_filters import is_lalala_configured
from .auth import require_user, require_user_json
from .media import resolve_job_path, stop_ffmpeg_process, transcode_to_mp3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lalal", tags=["lalal"])

# Constants
_LALAL_AUTH_VALIDATION_CACHE_SECONDS = 300
_MAX_EMAIL_LENGTH = 320
_MAX_ACTIVATION_KEY_LENGTH = 1024
_EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
# Formats handed to Lalal.ai unchanged. Anything else - the .source.opus /
# .webm / .m4a a finished audio job normally holds - is decoded to WAV first.
# Lalal.ai returns every stem in the format it received, so uploading Opus came
# back as Opus; re-encoding to MP3 *before* the upload would instead stack a
# second lossy generation ahead of the separation, which is exactly where
# quality must not be lost. WAV keeps the source audio bit-identical and the
# stems are encoded to MP3 afterwards.
_UPLOAD_READY_SUFFIXES: frozenset[str] = frozenset({".wav", ".flac"})
_FFMPEG_TIMEOUT_SECONDS = 900


async def _run_ffmpeg(cmd: list[str], *, description: str) -> None:
    """Run an ffmpeg command, raising RuntimeError on timeout or failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS
        )
    except TimeoutError:
        await stop_ffmpeg_process(proc)
        raise RuntimeError(f"{description} timed out") from None
    except BaseException:
        # BaseException, not Exception: the caller wraps this in an
        # asyncio.timeout well below _FFMPEG_TIMEOUT_SECONDS, so the realistic
        # exit is a CancelledError - which would otherwise leave ffmpeg running
        # past the request that started it.
        await stop_ffmpeg_process(proc)
        raise
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"{description} failed: {detail}")


async def _decode_for_upload(
    source_path: Path, output_dir: Path, base_name: str
) -> tuple[Path, Path | None]:
    """Return (file_to_upload, temp_file_to_clean_up_or_None)."""
    if source_path.suffix.lower() in _UPLOAD_READY_SUFFIXES:
        return source_path, None

    temp_path = output_dir / f"{base_name}.lalalsrc.{uuid.uuid4().hex[:8]}.wav"
    try:
        # Same governor budget the MP3 transcodes use: an unbounded number of
        # concurrent decodes is the one part of a split that can saturate a
        # small VPS on its own.
        async with governor.transcode_semaphore:
            await _run_ffmpeg(
                [
                    "ffmpeg", "-y",
                    "-i", str(source_path),
                    "-vn",
                    "-codec:a", "pcm_s16le",
                    "-f", "wav",
                    str(temp_path),
                ],
                description="Decoding source audio for Lalal.ai",
            )
    except BaseException:
        # The caller only learns the path on success, so cleanup belongs here:
        # a failed or cancelled decode would otherwise leave an uncompressed
        # WAV behind that nothing ever removes.
        with suppress(OSError):
            await asyncio.shield(asyncio.to_thread(temp_path.unlink, True))
        raise
    return temp_path, temp_path


async def _finalize_stem(source: Path, target: Path) -> None:
    """Move or transcode a returned stem to its final MP3 path."""
    if source == target:
        return
    if source.suffix.lower() == ".mp3":
        await asyncio.to_thread(source.replace, target)
        return
    await transcode_to_mp3(source, target)
    await asyncio.to_thread(source.unlink, True)


_STEM_VOCALS = "vocals"
_STEM_INSTRUMENTAL = "instrumental"
_VALID_STEMS = frozenset({_STEM_VOCALS, _STEM_INSTRUMENTAL})
_LALAL_PROCESS_TIMEOUT_SECONDS = 600

# Module-level state
_DATA_DIR: Path | None = None
_queue_event: Callable[[dict[str, Any]], None] | None = None


def init_lalal(
    data_dir: Path,
    queue_event_func: Callable[[dict[str, Any]], None],
) -> None:
    """Initialize the LALAL module with required dependencies."""
    global _DATA_DIR, _queue_event
    _DATA_DIR = data_dir
    _queue_event = queue_event_func



def _require_initialized() -> Path:
    if _DATA_DIR is None:
        raise RuntimeError("Lalal routes are not initialized")
    return _DATA_DIR.resolve()


def _validate_email(email: str) -> None:
    if len(email) > _MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=400, detail="Valid email address is required")


def _validate_stem(stem: str) -> None:
    if stem not in _VALID_STEMS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stem type. Use '{_STEM_VOCALS}' or '{_STEM_INSTRUMENTAL}'",
        )


def _validate_trim_id(trim_id: str) -> str:
    if not TRIM_ID_RE.fullmatch(trim_id):
        raise HTTPException(status_code=400, detail="Invalid trim_id format")
    return trim_id




def _newest_first(output_dir: Path, pattern: str) -> list[Path]:
    """Match ``pattern`` newest-first, skipping entries that vanish mid-scan.

    glob() and stat() are two trips to the filesystem; a trim deleted between
    them would otherwise surface as an uncaught FileNotFoundError.
    """
    candidates: list[tuple[float, Path]] = []
    for path in output_dir.glob(pattern):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if stat_module.S_ISREG(stat_result.st_mode):
            candidates.append((stat_result.st_mtime, path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


async def _path_is_ready_file(path: Path) -> bool:
    def _check() -> bool:
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return False
        return stat_module.S_ISREG(stat_result.st_mode) and stat_result.st_size > 0

    return await asyncio.to_thread(_check)


@asynccontextmanager
async def _acquire_processing_lock(lock_file: Path) -> AsyncIterator[None]:
    def _lock() -> int:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise HTTPException(status_code=409, detail="Lalal processing already in progress") from exc
        return lock_fd

    def _unlock(lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    lock_fd = await asyncio.to_thread(_lock)
    try:
        yield
    finally:
        await asyncio.to_thread(_unlock, lock_fd)


async def _remove_split_outputs(*paths: Path) -> None:
    """Remove any outputs left by a failed split attempt."""
    async def _remove(path: Path) -> None:
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError:
            logger.warning("Unable to remove failed Lalal output %s", path, exc_info=True)

    await asyncio.gather(*(_remove(path) for path in paths))


@asynccontextmanager
async def _processing_attempt(lock_file: Path, *outputs: Path) -> AsyncIterator[None]:
    """Hold the processing lock and clean up outputs before releasing it.

    Cleaning up outside the lock would let a request that never owned it - a
    caller bounced with 409, for instance - delete the outputs of the request
    that is still working on them.
    """
    async with _acquire_processing_lock(lock_file):
        try:
            yield
        except BaseException:
            await _remove_split_outputs(*outputs)
            raise


async def _get_cached_split_response(
    job_id_str: str,
    stem: str,
    vocals_path: Path,
    instrumental_path: Path,
    *,
    base_name: str,
    bpm: Any,
    trimmed: bool,
    trim_id: str | None,
) -> dict[str, Any] | None:
    vocals_exists, instrumental_exists = await asyncio.gather(
        _path_is_ready_file(vocals_path),
        _path_is_ready_file(instrumental_path),
    )
    if not (vocals_exists and instrumental_exists):
        return None

    if trimmed:
        logger.debug("Returning cached Lalal result for %s (stem=%s, trim_id=%s)", job_id_str, stem, trim_id)
    await _mark_lalal_split_done_if_ready(
        job_id_str,
        vocals_path,
        instrumental_path,
        trimmed=trimmed,
    )
    return _make_split_response(
        job_id_str,
        stem,
        trimmed=trimmed,
        filename=stem_download_name(base_name, stem, bpm),
        cached=True,
        trim_id=trim_id,
    )


async def _latest_trim_input(output_dir: Path, trim_id: str | None) -> tuple[Path, str]:
    if trim_id is not None:
        trim_id = _validate_trim_id(trim_id)
        file_path = output_dir / f"trim_{trim_id}.wav"
        if not await path_is_file(file_path):
            raise HTTPException(status_code=404, detail=f"Trim file not found: trim_{trim_id}.wav")
        return file_path, trim_id

    trim_files = await asyncio.to_thread(_newest_first, output_dir, "trim_*.wav")
    if not trim_files:
        raise HTTPException(status_code=400, detail="No trimmed file found. Please trim the audio first.")
    if len(trim_files) > 1:
        raise HTTPException(status_code=400, detail="trim_id required when multiple trims exist")

    file_path = trim_files[0]
    if not await path_is_file(file_path):
        raise HTTPException(status_code=404, detail="Trim file no longer exists")
    return file_path, file_path.stem.removeprefix("trim_")


async def _latest_trim_result(output_dir: Path, stem: str, trim_id: str | None) -> tuple[Path, str]:
    if trim_id is not None:
        trim_id = _validate_trim_id(trim_id)
        base_name = f"trim_{trim_id}"
        stem_path = output_dir / f"{base_name}_{stem}.mp3"
        if not await path_is_file(stem_path):
            raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file not found. Please process with Lalal.ai first.")
        return stem_path, base_name

    stem_files = await asyncio.to_thread(_newest_first, output_dir, f"trim_*_{stem}.mp3")
    if not stem_files:
        raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file not found. Please process with Lalal.ai first.")
    if len(stem_files) > 1:
        raise HTTPException(status_code=400, detail="trim_id required when multiple trims exist")

    stem_path = stem_files[0]
    if not await path_is_file(stem_path):
        raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file no longer exists")
    base_name = stem_path.stem[: -len(f"_{stem}")]
    return stem_path, base_name


def _build_download_url(job_id_str: str, stem: str, *, trimmed: bool, trim_id: str | None = None) -> str:
    query_parts = [f"stem={stem}"]
    if trimmed:
        query_parts.append("trimmed=true")
        if trim_id:
            query_parts.append(f"trim_id={trim_id}")
    return f"/api/lalal/download/{job_id_str}?{'&'.join(query_parts)}"


def _make_split_response(
    job_id_str: str,
    stem: str,
    *,
    trimmed: bool,
    filename: str,
    cached: bool,
    trim_id: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "cached": cached,
        "download_url": _build_download_url(job_id_str, stem, trimmed=trimmed, trim_id=trim_id),
        "filename": filename,
    }
    if trim_id:
        response["trim_id"] = trim_id
    return response


async def _mark_lalal_split_done_if_ready(
    job_id_str: str,
    vocals_path: Path,
    instrumental_path: Path,
    *,
    trimmed: bool,
) -> None:
    if trimmed:
        return
    vocals_exists, instrumental_exists = await asyncio.gather(
        _path_is_ready_file(vocals_path),
        _path_is_ready_file(instrumental_path),
    )
    if vocals_exists and instrumental_exists:
        await asyncio.to_thread(update_job, job_id_str, lalal_split_done=1)


class ActivationKeyRequest(BaseModel):
    """JSON body for the manual Lalal.ai activation-key endpoint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: Annotated[str, StringConstraints(min_length=3, max_length=_MAX_EMAIL_LENGTH)]
    activation_key: Annotated[
        str, StringConstraints(min_length=1, max_length=_MAX_ACTIVATION_KEY_LENGTH)
    ]


class AuthStatusResponse(BaseModel):
    """Saved Lalal.ai credentials plus their last validation result."""

    ok: Literal[True] = True
    configured: bool
    email: str
    token_valid: bool
    validation_error: str
    validated_at: int


class OperationResponse(BaseModel):
    """Acknowledgement for the auth mutation endpoints."""

    ok: Literal[True] = True
    message: str


# Storing auth settings is a read, a network round trip, then a write. WORKERS
# is pinned to 1 (docker/entrypoint.sh), so a process-local guard is enough:
# every credential change bumps the generation, and a validation that started
# against an older generation drops its result instead of overwriting newer
# credentials - or resurrecting ones that were just logged out.
_auth_state_lock = asyncio.Lock()
_auth_generation = 0


async def _commit_auth_settings(
    values: dict[str, Any], *, expected_generation: int | None, bump: bool
) -> bool:
    """Persist auth settings unless the credentials changed meanwhile."""
    global _auth_generation

    async with _auth_state_lock:
        if expected_generation is not None and _auth_generation != expected_generation:
            return False
        await asyncio.to_thread(set_settings, values)
        if bump:
            _auth_generation += 1
        return True


# ============================================================================
# Routes
# ============================================================================


@router.get("/status", response_model=AuthStatusResponse)
@limiter.limit("30/minute")
async def api_lalal_status(
    request: Request, force_refresh: bool = False, _user: str = Depends(require_user)
) -> AuthStatusResponse:
    """Return saved Lalal.ai auth status and validation state."""
    _ = request
    generation = _auth_generation
    settings = await asyncio.to_thread(get_settings, include_secrets=True)
    email = str(settings.get("lalalaai_email", "")).strip()
    auth_key = str(settings.get("lalalaai_auth_key", "")).strip()

    if not is_lalala_configured(settings):
        return AuthStatusResponse(
            configured=False,
            email="",
            token_valid=False,
            validation_error="",
            validated_at=0,
        )

    now_ts = int(time())
    checked_at = int(settings.get("lalalaai_auth_checked_at", 0) or 0)
    token_valid = bool(settings.get("lalalaai_auth_is_valid", False))
    validation_error = str(settings.get("lalalaai_auth_last_error", "") or "").strip()

    should_validate = (
        force_refresh
        or checked_at <= 0
        or (now_ts - checked_at) >= _LALAL_AUTH_VALIDATION_CACHE_SECONDS
    )

    if should_validate:
        from ..lalal import LalalClient

        token_valid = False
        validation_error = ""
        client = LalalClient(auth_key)
        try:
            async with client:
                await asyncio.wait_for(client.check_quota(), timeout=20.0)
            token_valid = True
        except Exception:
            logger.exception("Lalal status validation failed")
            token_valid = False
            validation_error = "Authentication validation failed"

        checked_at = now_ts
        # A key saved or cleared while this validation ran makes the result
        # describe credentials that are no longer stored - report it, but do
        # not stamp it onto the newer ones.
        await _commit_auth_settings(
            {
                "lalalaai_auth_checked_at": checked_at,
                "lalalaai_auth_is_valid": token_valid,
                "lalalaai_auth_last_error": validation_error,
            },
            expected_generation=generation,
            bump=False,
        )

    return AuthStatusResponse(
        configured=True,
        email=email,
        token_valid=token_valid,
        validation_error=validation_error,
        validated_at=checked_at,
    )


@router.post("/auth/activation-key", response_model=OperationResponse)
@limiter.limit("5/minute")
async def api_lalal_auth_activation_key(
    request: Request,
    body: ActivationKeyRequest,
    _user: str = Depends(require_user),
) -> OperationResponse:
    """Validate and store a manually provided Lalal activation key."""
    _ = request

    generation = _auth_generation
    email = body.email.lower()
    activation_key = body.activation_key

    _validate_email(email)

    from ..lalal import LalalClient, LalalError

    client = LalalClient(activation_key)
    try:
        async with client:
            await asyncio.wait_for(client.check_quota(), timeout=20.0)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Lalal.ai validation timed out") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Lalal.ai validation timed out") from exc
    except LalalError as exc:
        raise HTTPException(status_code=400, detail="Invalid activation key") from exc
    except (httpx.RequestError, OSError) as exc:
        logger.warning("Lalal activation-key validation unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Lalal.ai is temporarily unavailable") from exc
    except Exception as exc:
        logger.exception("Unexpected Lalal activation-key validation failure")
        raise HTTPException(status_code=503, detail="Lalal.ai is temporarily unavailable") from exc

    saved = await _commit_auth_settings(
        {
            "lalalaai_email": email,
            "lalalaai_auth_key": activation_key,
            "lalalaai_auth_checked_at": int(time()),
            "lalalaai_auth_is_valid": True,
            "lalalaai_auth_last_error": "",
        },
        expected_generation=generation,
        bump=True,
    )
    if not saved:
        raise HTTPException(
            status_code=409,
            detail="Lalal.ai authentication changed during validation",
        )

    return OperationResponse(message="Activation key saved")


@router.post("/auth/logout", response_model=OperationResponse)
@limiter.limit("10/minute")
async def api_lalal_auth_logout(
    request: Request, _user: str = Depends(require_user)
) -> OperationResponse:
    """Clear Lalal.ai auth credentials."""
    _ = request
    await _commit_auth_settings(
        {
            "lalalaai_email": "",
            "lalalaai_auth_key": "",
            "lalalaai_auth_checked_at": 0,
            "lalalaai_auth_is_valid": False,
            "lalalaai_auth_last_error": "",
        },
        expected_generation=None,
        bump=True,
    )

    return OperationResponse(message="Logged out")


@router.post("/{job_id}")
@limiter.limit("5/minute")
async def lalal_split(
    request: Request,
    job_id: uuid.UUID,
    stem: str = _STEM_VOCALS,
    trimmed: bool = False,
    trim_id: str | None = None,
    _user: str = Depends(require_user_json),
):
    """Split audio using Lalal.ai API.

    Note: Lalal always processes a vocals/instrumental split; `stem`
    only selects which cached result is returned.
    """
    data_dir = _require_initialized()
    if _queue_event is None:
        raise RuntimeError("Lalal routes are not initialized")

    _validate_stem(stem)

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in DOWNLOADABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Job not ready")

    if job["type"] != "audio":
        raise HTTPException(status_code=400, detail="Lalal.ai only works with audio jobs")

    output_dir = data_dir / job_id_str
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)

    if trimmed:
        file_path, trim_id = await _latest_trim_input(output_dir, trim_id)
        base_name = f"trim_{trim_id}"
    else:
        raw_filename = job["filename"]
        file_path = resolve_job_path(raw_filename)
        if not await path_is_file(file_path):
            raise HTTPException(status_code=404, detail="Source file not found")
        base_name = file_path.stem

    vocals_path = output_dir / f"{base_name}_vocals.mp3"
    instrumental_path = output_dir / f"{base_name}_instrumental.mp3"

    cached_response = await _get_cached_split_response(
        job_id_str,
        stem,
        vocals_path,
        instrumental_path,
        base_name=base_name,
        bpm=job["bpm"],
        trimmed=trimmed,
        trim_id=trim_id,
    )
    if cached_response is not None:
        return cached_response

    logger.info("Starting Lalal.ai processing for %s (stem=%s, trim_id=%s)", job_id, stem, trim_id)
    lock_file = output_dir / f".lalal_{base_name}.lock"

    try:
        from ..lalal import LalalClient, LalalError, StemType
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Lalal.ai module not available") from exc

    settings = await asyncio.to_thread(get_settings, include_secrets=True)
    auth_key = settings.get("lalalaai_auth_key")
    if not isinstance(auth_key, str) or not auth_key.strip():
        raise HTTPException(status_code=400, detail="Lalal.ai API key not configured")
    client = LalalClient(auth_key.strip())

    loop = asyncio.get_running_loop()
    queue_event = _queue_event
    if queue_event is None:
        raise RuntimeError("Lalal routes are not initialized")

    def sync_progress_callback(stage: str, pct: int) -> None:
        payload = {
            "type": "lalal_progress",
            "job_id": job_id_str,
            "stem": stem,
            "stage": stage,
            "progress": pct,
        }
        loop.call_soon_threadsafe(queue_event, payload)

    try:
        stem_type = StemType.VOCALS
        download_stem = True
        download_backing = True

        async with _processing_attempt(lock_file, vocals_path, instrumental_path):
            cached_response = await _get_cached_split_response(
                job_id_str,
                stem,
                vocals_path,
                instrumental_path,
                base_name=base_name,
                bpm=job["bpm"],
                trimmed=trimmed,
                trim_id=trim_id,
            )
            if cached_response is not None:
                return cached_response

            async with client:
                async with asyncio.timeout(_LALAL_PROCESS_TIMEOUT_SECONDS):
                    upload_path, temp_upload = await _decode_for_upload(
                        file_path, output_dir, base_name
                    )
                    try:
                        results = await client.process_file(
                            upload_path,
                            output_dir,
                            stem=stem_type,
                            download_stem=download_stem,
                            download_backing=download_backing,
                            progress_callback=sync_progress_callback,
                        )
                    finally:
                        if temp_upload is not None:
                            await asyncio.to_thread(temp_upload.unlink, True)

                    # Stems come back in the uploaded (WAV) format; convert them
                    # to the MP3 paths the cache lookup and downloads expect.
                    if "stem" in results:
                        await _finalize_stem(results["stem"], vocals_path)
                        results["stem"] = vocals_path
                    if "backing" in results:
                        await _finalize_stem(results["backing"], instrumental_path)
                        results["backing"] = instrumental_path

                    # Both stems are always requested, and both the cache
                    # lookup and lalal_split_done need both. Raise inside the
                    # lock so a half-finished pair is cleaned up rather than
                    # reported as a success nothing can reuse.
                    if "stem" not in results or "backing" not in results:
                        raise HTTPException(
                            status_code=500,
                            detail="Processing completed but no output file",
                        )

        await _mark_lalal_split_done_if_ready(
            job_id_str,
            vocals_path,
            instrumental_path,
            trimmed=trimmed,
        )

        return _make_split_response(
            job_id_str,
            stem,
            trimmed=trimmed,
            filename=stem_download_name(base_name, stem, job["bpm"]),
            cached=False,
            trim_id=trim_id,
        )

    # Output cleanup happens inside _processing_attempt, under the lock.
    except TimeoutError as exc:
        logger.error("Lalal.ai processing timed out for job %s", job_id)
        raise HTTPException(status_code=504, detail="Lalal.ai processing timed out") from exc
    except LalalError as exc:
        logger.exception("Lalal.ai error for job %s", job_id)
        raise HTTPException(status_code=502, detail="Lalal.ai processing failed") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error in Lalal.ai processing for job %s", job_id)
        raise HTTPException(status_code=500, detail="Processing failed") from exc


@router.get("/download/{job_id}")
@limiter.limit("10/minute")
async def lalal_download(
    request: Request,
    job_id: uuid.UUID,
    stem: str = _STEM_VOCALS,
    trimmed: bool = False,
    trim_id: str | None = None,
    _user: str = Depends(require_user_json),
):
    """Download processed Lalal.ai stem file.

    Args:
        job_id: The job UUID
        stem: Type of stem to download ('vocals' or 'instrumental')
        trimmed: If True, download the trimmed version.
        trim_id: Optional specific trim ID when trimmed=True.
    """
    _ = request
    data_dir = _require_initialized()

    _validate_stem(stem)

    job_id_str = str(job_id)
    job = await asyncio.to_thread(get_job, job_id_str)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = data_dir / job_id_str

    if trimmed:
        stem_path, base_name = await _latest_trim_result(output_dir, stem, trim_id)
    else:
        raw_filename = job["filename"]
        source_path = resolve_job_path(raw_filename)
        if not await path_is_file(source_path):
            raise HTTPException(status_code=404, detail="Source file not found")
        base_name = source_path.stem
        stem_path = output_dir / f"{base_name}_{stem}.mp3"
        if not await path_is_file(stem_path):
            raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file not found. Please process with Lalal.ai first.")

    return FileResponse(
        path=stem_path,
        filename=stem_download_name(base_name, stem, job["bpm"]),
        media_type="audio/mpeg",
    )
