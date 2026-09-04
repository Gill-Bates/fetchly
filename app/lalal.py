#!/usr/bin/env python3
#
# app/lalal.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Lalal.ai API client for audio stem separation.

OpenAPI spec: https://www.lalal.ai/api/v1/openapi.json
"""

import asyncio
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from numbers import Real
from pathlib import Path
from typing import Any, Self, TypedDict

import httpx

from .db import get_settings

logger = logging.getLogger(__name__)

LALAL_API_BASE = "https://www.lalal.ai"
LALAL_API_PREFIX = "/api/v1"
_TRANSFER_CHUNK_SIZE = 65536
_DEFAULT_LALAL_MAX_DOWNLOAD_GIB = 4
_BYTES_PER_GIB = 1024 * 1024 * 1024

type ProgressCallback = Callable[[int], None]
type StageProgressCallback = Callable[[str, int], None]
type DownloadProgressCallback = Callable[[int, int], None]


class StemType(StrEnum):
    """Stem separation target types.

    Note: ``INSTRUMENTAL`` maps to the API value ``"drum"``. Lalal.ai uses
    that value for full instrumental extraction, while ``DRUMS``
    (``"drums"``) targets percussion only.
    """

    VOCALS = "vocals"
    INSTRUMENTAL = "drum"  # API uses 'drum' internally for instrumental
    DRUMS = "drums"
    BASS = "bass"
    PIANO = "piano"
    ELECTRIC_GUITAR = "electric_guitar"
    ACOUSTIC_GUITAR = "acoustic_guitar"
    SYNTHESIZER = "synthesizer"
    VOICE = "voice"
    MUSIC = "music"
    STRINGS = "strings"
    WIND = "wind"


class SplitType(StrEnum):
    """Split configuration types."""

    AUTO = "auto"
    # Andromeda (v6) - current generation, the default used by the Lalal.ai web
    # UI. This is what the app sends unless a caller overrides it; separation
    # quality noticeably regresses on the older networks below.
    ANDROMEDA = "andromeda"
    # Previous generations, kept so existing callers/settings keep working.
    PHOENIX = "phoenix"
    ORION = "orion"
    PERSEUS = "perseus"
    CASSIOPEIA = "cassiopeia"
    LYNX = "lynx"
    LYRA = "lyra"


class ExtractionLevel(StrEnum):
    """Extraction intensity for compatible split modes."""

    DEEP_EXTRACTION = "deep_extraction"
    CLEAR_CUT = "clear_cut"


class SplitMode(StrEnum):
    """Processing mode/endpoint family in Lalal.ai v1 API."""

    STEM_SEPARATOR = "stem_separator"
    VOICE_CLEAN = "voice_clean"
    DEMUSER = "demuser"


class LalalError(Exception):
    """Base exception for Lalal.ai API errors."""



class LalalQuotaError(LalalError):
    """Quota/credits exceeded."""



class LalalUploadError(LalalError):
    """File upload failed."""



class LalalProcessingError(LalalError):
    """Processing failed."""



class UploadResult(TypedDict):
    id: str
    name: str
    duration: float
    size: int


class SplitResult(TypedDict):
    stem_track: str  # URL to download stem (e.g. vocals)
    back_track: str  # URL to download backing track (e.g. instrumental)
    stem_track_size: int
    back_track_size: int
    duration: float


_ALLOWED_DOWNLOAD_SCHEMES: frozenset[str] = frozenset({"https"})
_ALLOWED_DOWNLOAD_HOSTS: frozenset[str] = frozenset({
    "cdn.lalal.ai",
    "storage.lalal.ai",
    "s3.amazonaws.com",
})
_ALLOWED_DOWNLOAD_HOST_SUFFIXES: tuple[str, ...] = (".lalal.ai", ".amazonaws.com")


def _safe_header_filename(name: str) -> str:
    """Sanitize a filename for use inside HTTP header values."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return cleaned or "upload"


def _stringify_error_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        nested = value.get("detail")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
        nested_code = value.get("code")
        if isinstance(nested_code, str) and nested_code.strip():
            return nested_code.strip()
        return str(value) if value else None
    if isinstance(value, list) and value:
        return "; ".join(str(item) for item in value)
    return None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Return the response body as a dict, or ``{}`` when it is not a JSON object.

    Proxies and CDNs answer errors with HTML, so ``response.json()`` raises on
    exactly the responses callers want to report on. Folding an unparseable
    body into the "no payload" case lets the status check pick the LalalError.
    """
    try:
        payload = response.json()
    except ValueError:
        logger.debug(
            "Non-JSON response from %s (HTTP %s, content-type %s)",
            response.url,
            response.status_code,
            response.headers.get("content-type", "unknown"),
        )
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_api_error(data: dict[str, Any]) -> str:
    """Extract a human-readable error message from Lalal.ai API variants."""
    for key in ("message", "detail", "error", "errors", "code"):
        message = _stringify_error_value(data.get(key))
        if message:
            return message
    return "Unknown error"


def parse_minutes_left(quota: object) -> float | None:
    """Read the remaining processing minutes from a check_quota() payload.

    The API answers ``{"minutes_left": 261.5}``. A missing or unusable value is
    "unknown" (None), not zero - the web-session client has no such endpoint.
    """
    if not isinstance(quota, dict):
        return None

    value = quota.get("minutes_left")
    if isinstance(value, bool) or not isinstance(value, Real):
        return None

    minutes = float(value)
    if not math.isfinite(minutes) or minutes < 0:
        return None
    return minutes


def _extract_processing_error(error: Any, default: str = "Processing failed") -> str:
    """Normalize processing error payloads into a readable string."""
    message = _stringify_error_value(error)
    return message or default


def _max_result_download_bytes() -> int:
    """Return the persisted maximum Lalal result size in bytes."""
    try:
        settings = get_settings()
    except Exception:
        logger.warning("Could not read Lalal download limit; falling back to default", exc_info=True)
        return _DEFAULT_LALAL_MAX_DOWNLOAD_GIB * _BYTES_PER_GIB

    max_download_gib = max(1, int(settings.get("lalal_max_download_gib", _DEFAULT_LALAL_MAX_DOWNLOAD_GIB)))
    return max_download_gib * _BYTES_PER_GIB


async def _iter_file_chunks(file_path: Path, chunk_size: int = _TRANSFER_CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Yield file contents in bounded chunks without loading the full file into memory."""
    with file_path.open("rb") as file_handle:
        while True:
            chunk = await asyncio.to_thread(file_handle.read, chunk_size)
            if not chunk:
                break
            yield chunk


def _content_length(response: httpx.Response) -> int | None:
    """Return a validated response length, when the server provided one."""
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise LalalError("Invalid Content-Length returned by provider") from exc
    if length < 0:
        raise LalalError("Invalid Content-Length returned by provider")
    return length


def _is_safe_download_url(url: str) -> bool:
    """Return True only for HTTPS URLs on known Lalal.ai / CDN hosts."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return False
    if parsed.scheme not in _ALLOWED_DOWNLOAD_SCHEMES:
        return False
    host = parsed.host
    return host in _ALLOWED_DOWNLOAD_HOSTS or any(
        host.endswith(suffix) for suffix in _ALLOWED_DOWNLOAD_HOST_SUFFIXES
    )


class _BaseLalalClient:
    """Shared connection lifecycle for all Lalal.ai client variants."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    def _make_client(self) -> httpx.AsyncClient:  # pragma: no cover
        raise NotImplementedError

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared AsyncClient, creating it on first use."""
        async with self._client_lock:
            if self._client is None:
                self._client = self._make_client()
            return self._client

    def _progress_log_label(self) -> str:
        return "Lalal.ai processing"

    def _task_state(self, task_info: dict[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    def _task_progress(self, task_info: dict[str, Any]) -> int:  # pragma: no cover
        raise NotImplementedError

    def _task_error(self, task_info: dict[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    def _build_split_result(self, task_info: dict[str, Any]) -> SplitResult:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def __aenter__(self) -> Self:
        await self._get_client()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def wait_for_completion(
        self,
        task_id: str,
        *,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        progress_callback: ProgressCallback | None = None,
    ) -> SplitResult:
        """Poll the Lalal.ai API until processing completes or times out."""
        start_time = time.monotonic()

        while True:
            if time.monotonic() - start_time > timeout:
                raise LalalProcessingError(f"Processing timed out after {timeout}s")

            task_info = await self.check_progress(task_id)
            state = self._task_state(task_info)

            if state in {"error", "server_error", "cancelled"}:
                raise LalalProcessingError(self._task_error(task_info))

            if state == "success":
                return self._build_split_result(task_info)

            progress_pct = self._task_progress(task_info)
            if progress_callback:
                try:
                    progress_callback(progress_pct)
                except Exception:
                    logger.debug("Progress callback failed", exc_info=True)

            logger.debug("%s: %d%% (state=%s)", self._progress_log_label(), progress_pct, state)
            await asyncio.sleep(poll_interval)

    async def download_result(
        self,
        url: str,
        output_path: Path | str,
        *,
        progress_callback: DownloadProgressCallback | None = None,
    ) -> Path:
        """Download a processed audio file from a validated Lalal.ai URL."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading result to %s", output_path)

        if not _is_safe_download_url(url):
            raise LalalError("Unsafe download URL returned by API")

        part_path = output_path.with_suffix(f"{output_path.suffix}.part")
        downloaded = 0
        max_download_bytes = _max_result_download_bytes()
        try:
            async with (
                httpx.AsyncClient(timeout=120.0, follow_redirects=False) as transfer_client,
                transfer_client.stream("GET", url) as response,
            ):
                    response.raise_for_status()
                    total = _content_length(response)
                    if total is not None and total > max_download_bytes:
                        raise LalalError("Download exceeds configured size limit")

                    with part_path.open("wb") as output_file:
                        async for chunk in response.aiter_bytes(chunk_size=_TRANSFER_CHUNK_SIZE):
                            downloaded += len(chunk)
                            if downloaded > max_download_bytes:
                                raise LalalError("Download exceeds configured size limit")
                            await asyncio.to_thread(output_file.write, chunk)

                            if progress_callback and total is not None:
                                try:
                                    progress_callback(downloaded, total)
                                except Exception:
                                    logger.debug("Download progress callback failed", exc_info=True)

            part_path.replace(output_path)
            return output_path
        finally:
            part_path.unlink(missing_ok=True)


class LalalClient(_BaseLalalClient):
    """Async client for Lalal.ai API."""

    def __init__(self, api_key: str, timeout: float = 300.0) -> None:
        if not api_key:
            raise ValueError("API key is required")

        super().__init__(timeout)
        self._api_key = api_key
        self._headers = {
            "X-License-Key": api_key,
            "Authorization": f"license {api_key}",
        }

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=LALAL_API_BASE,
            headers=self._headers,
            timeout=self._timeout,
        )

    def _task_state(self, task_info: dict[str, Any]) -> str:
        return str(task_info.get("status", "unknown"))

    def _task_progress(self, task_info: dict[str, Any]) -> int:
        return int(task_info.get("progress") or 0)

    def _task_error(self, task_info: dict[str, Any]) -> str:
        return _extract_processing_error(task_info.get("error"), "Processing failed")

    def _build_split_result(self, task_info: dict[str, Any]) -> SplitResult:
        result_info = task_info.get("result", {})
        tracks = result_info.get("tracks", []) if isinstance(result_info, dict) else []

        stem_info: dict[str, Any] = {}
        back_info: dict[str, Any] = {}
        if isinstance(tracks, list):
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                track_type = track.get("type")
                if track_type == "stem" and not stem_info:
                    stem_info = track
                elif track_type == "back" and not back_info:
                    back_info = track

        return SplitResult(
            stem_track=stem_info.get("url", ""),
            back_track=back_info.get("url", ""),
            stem_track_size=int(stem_info.get("size") or 0),
            back_track_size=int(back_info.get("size") or 0),
            duration=float((result_info.get("duration") if isinstance(result_info, dict) else None) or 0),
        )

    async def check_quota(self) -> dict[str, Any]:
        """Check remaining API quota; ``minutes_left`` holds spendable minutes
        (see :func:`parse_minutes_left`).
        """
        client = await self._get_client()
        response = await client.post(f"{LALAL_API_PREFIX}/limits/minutes_left/", timeout=30.0)

        if response.status_code == 401:
            raise LalalError("Invalid API key")

        data = _json_object(response)
        if response.status_code >= 400:
            raise LalalError(_extract_api_error(data))
        return data

    async def upload_file(self, file_path: Path | str) -> UploadResult:
        """Upload an audio file for processing; returns its file ID and metadata."""
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = file_path.stat().st_size
        logger.info("Uploading %s (%d bytes) to Lalal.ai", file_path.name, file_size)

        client = await self._get_client()
        response = await client.post(
            f"{LALAL_API_PREFIX}/upload/",
            content=_iter_file_chunks(file_path),
            headers={
                "Content-Disposition": f'attachment; filename="{_safe_header_filename(file_path.name)}"',
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
            },
        )

        if response.status_code == 401:
            raise LalalError("Invalid API key")

        if response.status_code == 402:
            raise LalalQuotaError("Insufficient credits/quota")

        file_info = _json_object(response)

        if response.status_code >= 400:
            raise LalalUploadError(_extract_api_error(file_info))

        return UploadResult(
            id=file_info.get("id", ""),
            name=file_info.get("name", file_path.name),
            duration=float(file_info.get("duration", 0)),
            size=int(file_info.get("size", file_size)),
        )

    async def split(
        self,
        file_id: str,
        *,
        stem: StemType = StemType.VOCALS,
        split_type: SplitType = SplitType.ANDROMEDA,
        enhanced_processing: bool = True,
        split_mode: SplitMode = SplitMode.STEM_SEPARATOR,
        noise_cancelling_level: int | None = None,
        dereverb_enabled: bool | None = None,
        extraction_level: ExtractionLevel | str | None = None,
        multivocal: str | None = None,
    ) -> str:
        """Start stem separation for an uploaded ``file_id``; returns a task ID.

        ``noise_cancelling_level`` is 0-2, ``multivocal`` is ``"lead_back"`` or
        None. ``split_mode`` selects the endpoint family.
        """
        client = await self._get_client()
        if noise_cancelling_level is not None and not (0 <= noise_cancelling_level <= 2):
            raise ValueError("noise_cancelling_level must be between 0 and 2")

        extraction_value: str | None
        if extraction_level is None:
            extraction_value = None
        elif isinstance(extraction_level, ExtractionLevel):
            extraction_value = extraction_level.value
        else:
            extraction_value = str(extraction_level).strip().lower()
            if extraction_value not in {"deep_extraction", "clear_cut"}:
                raise ValueError("extraction_level must be one of: deep_extraction, clear_cut")

        presets: dict[str, Any] = {
            "splitter": split_type.value,
            "enhanced_processing_enabled": bool(enhanced_processing),
        }
        if dereverb_enabled is not None:
            presets["dereverb_enabled"] = dereverb_enabled

        if split_mode == SplitMode.STEM_SEPARATOR:
            presets["stem"] = stem.value
            if extraction_value is not None:
                presets["extraction_level"] = extraction_value
            if multivocal is not None:
                if multivocal != "lead_back":
                    raise ValueError('multivocal must be "lead_back" when provided')
                presets["multivocal"] = multivocal
        elif split_mode == SplitMode.VOICE_CLEAN:
            presets["stem"] = StemType.VOICE.value
            if noise_cancelling_level is not None:
                presets["noise_cancelling_level"] = noise_cancelling_level
        elif split_mode == SplitMode.DEMUSER:
            presets["stem"] = StemType.MUSIC.value

        payload = {
            "source_id": file_id,
            "presets": presets,
        }

        endpoint = {
            SplitMode.STEM_SEPARATOR: f"{LALAL_API_PREFIX}/split/stem_separator/",
            SplitMode.VOICE_CLEAN: f"{LALAL_API_PREFIX}/split/voice_clean/",
            SplitMode.DEMUSER: f"{LALAL_API_PREFIX}/split/demuser/",
        }[split_mode]

        response = await client.post(endpoint, json=payload, timeout=60.0)

        if response.status_code == 401:
            raise LalalError("Invalid API key")

        if response.status_code == 402:
            raise LalalQuotaError("Insufficient credits for processing")

        result = _json_object(response)
        if response.status_code >= 400:
            raise LalalProcessingError(_extract_api_error(result))

        task_id = str(result.get("task_id", "")).strip()
        if not task_id:
            raise LalalProcessingError("Split request failed: missing task_id")
        return task_id

    async def check_progress(self, task_id: str) -> dict[str, Any]:
        """Return the progress payload (state + percentage) for a split() task."""
        client = await self._get_client()
        response = await client.post(
            f"{LALAL_API_PREFIX}/check/",
            json={"task_ids": [task_id]},
            timeout=30.0,
        )

        if response.status_code == 401:
            raise LalalError("Invalid API key")

        result = _json_object(response)
        if response.status_code >= 400:
            raise LalalError(_extract_api_error(result))

        result_map = result.get("result", {})
        if not isinstance(result_map, dict):
            return {}
        task_info = result_map.get(task_id, {})
        return task_info if isinstance(task_info, dict) else {}

    async def process_file(
        self,
        input_path: Path | str,
        output_dir: Path | str,
        *,
        stem: StemType = StemType.VOCALS,
        split_type: SplitType = SplitType.ANDROMEDA,
        split_mode: SplitMode = SplitMode.STEM_SEPARATOR,
        noise_cancelling_level: int | None = None,
        dereverb_enabled: bool | None = None,
        extraction_level: ExtractionLevel | str | None = None,
        multivocal: str | None = None,
        download_stem: bool = True,
        download_backing: bool = True,
        progress_callback: StageProgressCallback | None = None,
    ) -> dict[str, Path]:
        """Full workflow (upload, process, download); returns ``{name: path}``.

        ``progress_callback`` is ``(stage: str, progress: int)``.
        """
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        def emit_progress(stage: str, pct: int) -> None:
            if progress_callback:
                try:
                    progress_callback(stage, pct)
                except Exception:
                    logger.debug("Progress callback failed", exc_info=True)

        # 1. Upload
        emit_progress("upload", 0)
        upload_result = await self.upload_file(input_path)
        file_id = upload_result["id"]
        emit_progress("upload", 100)

        # 2. Start processing
        emit_progress("processing", 0)
        task_id = await self.split(
            file_id,
            stem=stem,
            split_type=split_type,
            split_mode=split_mode,
            noise_cancelling_level=noise_cancelling_level,
            dereverb_enabled=dereverb_enabled,
            extraction_level=extraction_level,
            multivocal=multivocal,
        )

        # 3. Wait for completion
        def on_processing_progress(pct: int) -> None:
            emit_progress("processing", pct)

        split_result = await self.wait_for_completion(
            task_id,
            progress_callback=on_processing_progress,
        )
        emit_progress("processing", 100)

        # 4. Download results
        results: dict[str, Path] = {}
        base_name = input_path.stem
        # Lalal.ai returns each stem in the uploaded format; a hardcoded ".mp3"
        # would mislabel Opus payloads.
        result_suffix = input_path.suffix.lower() or ".mp3"

        if download_stem and split_result["stem_track"]:
            emit_progress("download_stem", 0)
            stem_path = output_dir / f"{base_name}_{stem.value}{result_suffix}"
            await self.download_result(split_result["stem_track"], stem_path)
            results["stem"] = stem_path
            emit_progress("download_stem", 100)

        if download_backing and split_result["back_track"]:
            emit_progress("download_backing", 0)
            backing_name = "instrumental" if stem == StemType.VOCALS else "backing"
            backing_path = output_dir / f"{base_name}_{backing_name}{result_suffix}"
            await self.download_result(split_result["back_track"], backing_path)
            results["backing"] = backing_path
            emit_progress("download_backing", 100)

        return results


class LalalWebSessionClient(_BaseLalalClient):
    """Experimental client using Lalal.ai website session credentials.

    Best-effort; may break when the web endpoints change. Prefer LalalClient
    with an official API key.
    """

    def __init__(self, session_cookie: str, csrf_token: str, timeout: float = 300.0) -> None:
        if not session_cookie.strip():
            raise ValueError("Session cookie is required")
        if not csrf_token.strip():
            raise ValueError("CSRF token is required")

        self._session_cookie = session_cookie.strip()
        self._csrf_token = csrf_token.strip()
        super().__init__(timeout)

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=LALAL_API_BASE,
            timeout=self._timeout,
            headers={
                "x-csrftoken": self._csrf_token,
                "origin": LALAL_API_BASE,
                "referer": f"{LALAL_API_BASE}/",
                "cookie": self._session_cookie,
            },
        )


    def _progress_log_label(self) -> str:
        return "Lalal.ai web processing"

    def _task_state(self, task_info: dict[str, Any]) -> str:
        task = task_info.get("task", {}) if isinstance(task_info, dict) else {}
        return str(task.get("state", "unknown"))

    def _task_progress(self, task_info: dict[str, Any]) -> int:
        task = task_info.get("task", {}) if isinstance(task_info, dict) else {}
        return int(task.get("progress", 0) or 0)

    def _task_error(self, task_info: dict[str, Any]) -> str:
        task = task_info.get("task", {}) if isinstance(task_info, dict) else {}
        return _extract_processing_error(task_info.get("error") or task, "Processing failed")

    def _build_split_result(self, task_info: dict[str, Any]) -> SplitResult:
        split_data = task_info.get("split") or task_info.get("preview") or {}
        if not isinstance(split_data, dict):
            split_data = {}

        stem_track = str(split_data.get("stem_track") or split_data.get("stem_track_playlist") or "")
        back_track = str(split_data.get("back_track") or split_data.get("back_track_playlist") or "")

        return SplitResult(
            stem_track=stem_track,
            back_track=back_track,
            stem_track_size=int(split_data.get("stem_track_size", 0) or 0),
            back_track_size=int(split_data.get("back_track_size", 0) or 0),
            duration=float(split_data.get("duration", task_info.get("duration", 0)) or 0),
        )

    async def check_quota(self) -> dict[str, Any]:
        """Best-effort quota check; the web API exposes no minutes_left endpoint."""
        client = await self._get_client()
        response = await client.post("/api/constraints/", data={"params": "[]"}, timeout=30.0)
        if response.status_code in {401, 403}:
            raise LalalError("Lalal.ai web session is invalid or expired")
        data = _json_object(response)
        if response.status_code >= 400:
            raise LalalError(f"Web session check failed: {_extract_api_error(data)}")
        return data or {"mode": "web_session"}

    async def upload_file(self, file_path: Path | str) -> UploadResult:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = file_path.stat().st_size
        client = await self._get_client()

        create_resp = await client.post(
            "/api/upload/multipart/create/",
            data={
                "file_name": file_path.name,
                "parts_count": "1",
            },
            timeout=60.0,
        )

        if create_resp.status_code in {401, 403}:
            raise LalalError("Lalal.ai web session is invalid or expired")
        if create_resp.status_code >= 400:
            raise LalalUploadError(f"Multipart create failed: HTTP {create_resp.status_code}")

        create_data = _json_object(create_resp)
        if create_data.get("status") != "success":
            raise LalalUploadError(f"Multipart create failed: {create_data}")

        file_id = str(create_data.get("file_id", "")).strip()
        upload_id = str(create_data.get("upload_id", "")).strip()
        upload_urls = create_data.get("upload_urls", [])
        if not file_id or not upload_id or not isinstance(upload_urls, list) or not upload_urls:
            raise LalalUploadError("Multipart create failed: missing upload metadata")

        upload_url = str(upload_urls[0]).strip()
        if not _is_safe_download_url(upload_url):
            raise LalalUploadError("Unsafe upload URL returned by API")

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as transfer_client:
            put_resp = await transfer_client.put(
                upload_url,
                content=_iter_file_chunks(file_path),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file_size),
                },
            )
        if put_resp.status_code >= 400:
            raise LalalUploadError(f"Multipart upload failed: HTTP {put_resp.status_code}")

        complete_resp = await client.post(
            "/api/upload/multipart/complete/",
            data={
                "file_id": file_id,
                "upload_id": upload_id,
            },
            timeout=60.0,
        )
        if complete_resp.status_code >= 400:
            raise LalalUploadError(f"Multipart complete failed: HTTP {complete_resp.status_code}")

        file_info = _json_object(complete_resp)
        if file_info.get("status") != "success":
            raise LalalUploadError(f"Multipart complete failed: {file_info}")

        return UploadResult(
            id=str(file_info.get("id", file_id)),
            name=str(file_info.get("name", file_path.name)),
            duration=float(file_info.get("duration", 0)),
            size=int(file_info.get("size", file_size)),
        )

    async def split(
        self,
        file_id: str,
        *,
        stem: StemType = StemType.VOCALS,
        split_type: SplitType = SplitType.ANDROMEDA,
        enhanced_processing: bool = True,
        split_mode: SplitMode = SplitMode.STEM_SEPARATOR,
        noise_cancelling_level: int | None = None,
        dereverb_enabled: bool | None = None,
        extraction_level: ExtractionLevel | str | None = None,
        multivocal: str | None = None,
    ) -> str:
        if extraction_level is not None:
            logger.warning("extraction_level is ignored in Lalal.ai web session mode")
        if split_mode != SplitMode.STEM_SEPARATOR:
            logger.warning(
                "split_mode=%s is ignored in Lalal.ai web session mode; using stem_separator",
                split_mode.value,
            )

        client = await self._get_client()
        if noise_cancelling_level is None:
            noise_cancelling_level = 1

        if multivocal is None:
            multivocal = ""

        response = await client.post(
            "/api/preview/",
            data={
                "id": file_id,
                "stem": stem.value,
                "splitter": split_type.value,
                "dereverb_enabled": "true" if bool(dereverb_enabled) else "false",
                "noise_cancelling_level": str(noise_cancelling_level),
                "enhanced_processing_enabled": "true" if enhanced_processing else "false",
                "multivocal": multivocal,
                "with_segments": "true",
                "turnstile-response": "",
            },
            timeout=60.0,
        )

        if response.status_code in {401, 403}:
            raise LalalError("Lalal.ai web session is invalid or expired")
        if response.status_code >= 400:
            raise LalalProcessingError(f"Preview request failed: HTTP {response.status_code}")

        data = _json_object(response)
        if data.get("status") != "success":
            raise LalalProcessingError(f"Preview request failed: {data}")

        task_id = str(data.get("task_id", "")).strip()
        if not task_id:
            raise LalalProcessingError("Preview request failed: missing task_id")
        return task_id

    async def check_progress(self, task_id: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.post("/api/check/", data={"id": task_id}, timeout=30.0)
        if response.status_code in {401, 403}:
            raise LalalError("Lalal.ai web session is invalid or expired")
        if response.status_code >= 400:
            raise LalalError(f"Web session check failed: HTTP {response.status_code}")

        data = _json_object(response)
        if data.get("status") != "success":
            raise LalalError(f"Web session check failed: {data}")

        result = data.get("result", {})
        if not isinstance(result, dict):
            return {}
        task_info = result.get(task_id, {})
        return task_info if isinstance(task_info, dict) else {}


def get_lalal_client() -> LalalClient | None:
    """LalalClient from the stored auth key, or None when not configured."""
    from .db import get_settings
    from .utils.template_filters import is_lalala_configured

    settings = get_settings(include_secrets=True)
    if not is_lalala_configured(settings):
        return None

    auth_key = str(settings.get("lalalaai_auth_key", "")).strip()
    return LalalClient(auth_key)


async def separate_vocals(
    input_path: Path | str,
    output_dir: Path | str,
    *,
    extract_vocals: bool = True,
    extract_instrumental: bool = True,
    progress_callback: StageProgressCallback | None = None,
) -> dict[str, Path]:
    """Separate vocals from instrumental; returns ``{'vocals'|'instrumental': path}``.

    Raises LalalError when the auth key is missing or processing fails.
    """
    try:
        client = await asyncio.to_thread(get_lalal_client)
    except Exception as exc:
        raise LalalError("Failed to load Lalal.ai configuration") from exc
    if not client:
        raise LalalError("Lalal.ai auth key is not configured")

    try:
        results = await client.process_file(
            input_path,
            output_dir,
            stem=StemType.VOCALS,
            download_stem=extract_vocals,
            download_backing=extract_instrumental,
            progress_callback=progress_callback,
        )
    finally:
        await client.close()

    output: dict[str, Path] = {}
    if "stem" in results:
        output["vocals"] = results["stem"]
    if "backing" in results:
        output["instrumental"] = results["backing"]

    return output
