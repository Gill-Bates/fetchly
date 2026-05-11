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
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..common.rate_limit import limiter
from ..db import get_job, get_settings, set_settings, update_job
from ..utils.template_filters import is_lalala_configured
from .auth import require_user, require_user_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lalal", tags=["lalal"])

# Constants
_LALAL_AUTH_VALIDATION_CACHE_SECONDS = 300
_MAX_EMAIL_LENGTH = 320
_EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
_TRIM_ID_RE = re.compile(r"^\d+_\d+$")
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


async def _get_json(request: Request) -> dict[str, Any]:
    """Parse request JSON and ensure the payload is an object."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return payload


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
    if not _TRIM_ID_RE.fullmatch(trim_id):
        raise HTTPException(status_code=400, detail="Invalid trim_id format")
    return trim_id


def _resolve_job_file(raw_filename: str | None) -> Path:
    data_dir = _require_initialized()
    if not raw_filename:
        raise HTTPException(status_code=404, detail="not ready")

    file_path = Path(str(raw_filename).strip())
    if not file_path.is_absolute():
        file_path = data_dir / file_path
    file_path = file_path.resolve()
    try:
        file_path.relative_to(data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="forbidden") from exc
    return file_path


async def _path_is_file(path: Path) -> bool:
    return await asyncio.to_thread(path.is_file)


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
            with suppress(OSError):
                os.unlink(lock_file)

    lock_fd = await asyncio.to_thread(_lock)
    try:
        yield
    finally:
        await asyncio.to_thread(_unlock, lock_fd)


async def _get_cached_split_response(
    job_id_str: str,
    stem: str,
    vocals_path: Path,
    instrumental_path: Path,
    *,
    trimmed: bool,
    trim_id: str | None,
) -> dict[str, Any] | None:
    vocals_exists, instrumental_exists = await asyncio.gather(
        _path_is_ready_file(vocals_path),
        _path_is_ready_file(instrumental_path),
    )
    if not (vocals_exists and instrumental_exists):
        return None

    cached_path = vocals_path if stem == _STEM_VOCALS else instrumental_path
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
        filename=cached_path.name,
        cached=True,
        trim_id=trim_id,
    )


async def _latest_trim_input(output_dir: Path, trim_id: str | None) -> tuple[Path, str]:
    if trim_id is not None:
        trim_id = _validate_trim_id(trim_id)
        file_path = output_dir / f"trim_{trim_id}.wav"
        if not await _path_is_file(file_path):
            raise HTTPException(status_code=404, detail=f"Trim file not found: trim_{trim_id}.wav")
        return file_path, trim_id

    def _find_latest() -> list[Path]:
        return sorted(
            output_dir.glob("trim_*.wav"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    trim_files = await asyncio.to_thread(_find_latest)
    if not trim_files:
        raise HTTPException(status_code=400, detail="No trimmed file found. Please trim the audio first.")
    if len(trim_files) > 1:
        raise HTTPException(status_code=400, detail="trim_id required when multiple trims exist")

    file_path = trim_files[0]
    return file_path, file_path.stem.removeprefix("trim_")


async def _latest_trim_result(output_dir: Path, stem: str, trim_id: str | None) -> tuple[Path, str]:
    if trim_id is not None:
        trim_id = _validate_trim_id(trim_id)
        base_name = f"trim_{trim_id}"
        stem_path = output_dir / f"{base_name}_{stem}.mp3"
        if not await _path_is_file(stem_path):
            raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file not found. Please process with Lalal.ai first.")
        return stem_path, base_name

    def _find_latest() -> list[Path]:
        return sorted(
            output_dir.glob(f"trim_*_{stem}.mp3"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    stem_files = await asyncio.to_thread(_find_latest)
    if not stem_files:
        raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file not found. Please process with Lalal.ai first.")
    if len(stem_files) > 1:
        raise HTTPException(status_code=400, detail="trim_id required when multiple trims exist")

    stem_path = stem_files[0]
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


# ============================================================================
# Routes
# ============================================================================


@router.get("/status")
@limiter.limit("30/minute")
async def api_lalal_status(request: Request, force_refresh: bool = False, _user: str = Depends(require_user)):
    """Return saved Lalal.ai auth status and validation state."""
    _ = request
    settings = await asyncio.to_thread(get_settings)
    email = str(settings.get("lalalaai_email", "")).strip()
    auth_key = str(settings.get("lalalaai_auth_key", "")).strip()

    if not is_lalala_configured(settings):
        return {
            "ok": True,
            "configured": False,
            "email": "",
            "token_valid": False,
            "validation_error": "",
            "validated_at": 0,
        }

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
        await asyncio.to_thread(
            set_settings,
            {
                "lalalaai_auth_checked_at": checked_at,
                "lalalaai_auth_is_valid": token_valid,
                "lalalaai_auth_last_error": validation_error,
            },
        )

    return {
        "ok": True,
        "configured": True,
        "email": email,
        "token_valid": token_valid,
        "validation_error": validation_error,
        "validated_at": checked_at,
    }


@router.post("/auth/activation-key")
@limiter.limit("5/minute")
async def api_lalal_auth_activation_key(request: Request, _user: str = Depends(require_user)):
    """Validate and store a manually provided Lalal activation key."""

    payload = await _get_json(request)

    email = str(payload.get("email", "")).strip().lower()
    activation_key = str(payload.get("activation_key", "")).strip()

    _validate_email(email)
    if not activation_key:
        raise HTTPException(status_code=400, detail="Activation key is required")

    from ..lalal import LalalClient

    client = LalalClient(activation_key)
    try:
        async with client:
            await asyncio.wait_for(client.check_quota(), timeout=20.0)
    except Exception:
        logger.exception("Invalid Lalal activation key")
        raise HTTPException(status_code=400, detail="Invalid activation key")

    await asyncio.to_thread(
        set_settings,
        {
            "lalalaai_email": email,
            "lalalaai_auth_key": activation_key,
            "lalalaai_auth_checked_at": int(time()),
            "lalalaai_auth_is_valid": True,
            "lalalaai_auth_last_error": "",
        },
    )

    return {"ok": True, "message": "Activation key saved"}


@router.post("/auth/logout")
@limiter.limit("10/minute")
async def api_lalal_auth_logout(request: Request, _user: str = Depends(require_user)):
    """Clear Lalal.ai auth credentials."""
    _ = request
    await asyncio.to_thread(
        set_settings,
        {
            "lalalaai_email": "",
            "lalalaai_auth_key": "",
            "lalalaai_auth_checked_at": 0,
            "lalalaai_auth_is_valid": False,
            "lalalaai_auth_last_error": "",
        },
    )

    return {"ok": True, "message": "Logged out"}


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

    if job["status"] not in ("done", "analysis", "analysis_done"):
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
        file_path = _resolve_job_file(raw_filename)
        if not await _path_is_file(file_path):
            raise HTTPException(status_code=404, detail="Source file not found")
        base_name = file_path.stem

    vocals_path = output_dir / f"{base_name}_vocals.mp3"
    instrumental_path = output_dir / f"{base_name}_instrumental.mp3"

    cached_response = await _get_cached_split_response(
        job_id_str,
        stem,
        vocals_path,
        instrumental_path,
        trimmed=trimmed,
        trim_id=trim_id,
    )
    if cached_response is not None:
        return cached_response

    logger.info("Starting Lalal.ai processing for %s (stem=%s, trim_id=%s)", job_id, stem, trim_id)
    lock_file = output_dir / f".lalal_{base_name}.lock"

    try:
        from ..lalal import LalalClient, LalalError, StemType, get_lalal_client
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Lalal.ai module not available") from exc

    client = get_lalal_client()
    if not client:
        raise HTTPException(status_code=400, detail="Lalal.ai API key not configured")

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

        async with _acquire_processing_lock(lock_file):
            cached_response = await _get_cached_split_response(
                job_id_str,
                stem,
                vocals_path,
                instrumental_path,
                trimmed=trimmed,
                trim_id=trim_id,
            )
            if cached_response is not None:
                return cached_response

            async with client:
                async with asyncio.timeout(_LALAL_PROCESS_TIMEOUT_SECONDS):
                    results = await client.process_file(
                        file_path,
                        output_dir,
                        stem=stem_type,
                        download_stem=download_stem,
                        download_backing=download_backing,
                        progress_callback=sync_progress_callback,
                    )

        if stem == _STEM_VOCALS and "stem" in results:
            result_path = results["stem"]
        elif stem == _STEM_INSTRUMENTAL and "backing" in results:
            result_path = results["backing"]
        else:
            raise HTTPException(status_code=500, detail="Processing completed but no output file")

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
            filename=result_path.name,
            cached=False,
            trim_id=trim_id,
        )

    except TimeoutError:
        logger.error("Lalal.ai processing timed out for job %s", job_id)
        raise HTTPException(status_code=504, detail="Lalal.ai processing timed out")
    except LalalError:
        logger.exception("Lalal.ai error for job %s", job_id)
        raise HTTPException(status_code=500, detail="Lalal.ai processing failed")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in Lalal.ai processing for job %s", job_id)
        raise HTTPException(status_code=500, detail="Processing failed")


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
        stem_path, _base_name = await _latest_trim_result(output_dir, stem, trim_id)
    else:
        raw_filename = job["filename"]
        source_path = _resolve_job_file(raw_filename)
        if not await _path_is_file(source_path):
            raise HTTPException(status_code=404, detail="Source file not found")
        stem_path = output_dir / f"{source_path.stem}_{stem}.mp3"
        if not await _path_is_file(stem_path):
            raise HTTPException(status_code=404, detail=f"{stem.capitalize()} file not found. Please process with Lalal.ai first.")

    return FileResponse(path=stem_path, filename=stem_path.name, media_type="audio/mpeg")
