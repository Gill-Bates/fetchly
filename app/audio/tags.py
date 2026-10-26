#!/usr/bin/env python3
#
# app/audio/tags.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""ID3 tagging for the MP3 files fetchly produces.

Cover and comment are written while the MP3 is transcoded; the tempo usually
arrives later (the analysis runs after the download), so :func:`ensure_bpm_tag`
(blocking) and :func:`ensure_bpm_tag_async` (cancellable from an event loop)
rewrite the finished file in place. All ffmpeg calls use argument lists and
write to a temp file next to the target, replaced atomically.
"""

import asyncio
import json
import logging
import re
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from .naming import SOURCE_MARKER

logger = logging.getLogger(__name__)

COMMENT_TEXT: Final[str] = "Downloaded with Fetchly - https://github.com/Gill-Bates/fetchly"
COVER_FILENAME: Final[str] = "thumbnail.jpg"
COVER_MAX_EDGE: Final[int] = 1280
COVER_FILTER: Final[str] = (
    f"scale=w='min(iw,{COVER_MAX_EDGE})':h='min(ih,{COVER_MAX_EDGE})'"
    ":force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuvj420p"
)
# A thumbnail is a few hundred KB; anything larger is not one worth embedding.
_COVER_MAX_BYTES: Final[int] = 8 * 1024 * 1024
_MAX_TAG_LENGTH: Final[int] = 256
_TAG_TIMEOUT_SECONDS: Final[int] = 120
# C0/C1 controls and DEL; ffmpeg would write them verbatim into the frame.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def mp3_path_for_source(source_path: Path) -> Path:
    """Return the MP3 path for an internal source, e.g. ``a.source.opus`` -> ``a.mp3``."""
    return source_path.parent / f"{source_path.stem.removesuffix(SOURCE_MARKER)}.mp3"


def sanitize_tag_value(value: object, *, max_length: int = _MAX_TAG_LENGTH) -> str:
    """Return *value* as a single-line, length-limited tag text."""
    text = _CONTROL_CHARS.sub(" ", str(value))
    return " ".join(text.split())[:max_length]


def bpm_to_int(bpm: Any) -> int | None:
    """Round a detected tempo for TBPM; ``None`` for "no usable tempo"."""
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)):
        return None
    rounded = round(bpm)
    return rounded if rounded > 0 else None


def find_cover(job_dir: Path) -> Path | None:
    """Return the job's yt-dlp thumbnail when it is a usable regular file."""
    cover = job_dir / COVER_FILENAME
    try:
        if cover.is_symlink() or not cover.is_file():
            return None
        size = cover.stat().st_size
    except OSError:
        return None
    return cover if 0 < size <= _COVER_MAX_BYTES else None


def build_mp3_transcode_cmd(
    source: Path,
    output: Path,
    *,
    cover: Path | None = None,
    bpm: Any = None,
) -> list[str]:
    """Build the ffmpeg command that makes the tagged MP3 from an audio source.

    Without *cover* no picture is mapped, so a missing or broken thumbnail
    never changes the audio result.
    """
    cmd = ["ffmpeg", "-y", "-i", str(source)]
    if cover is not None:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a:0"]
    if cover is not None:
        cmd += ["-map", "1:v:0"]
    cmd += ["-codec:a", "libmp3lame", "-q:a", "0", "-map_metadata", "0"]
    if cover is not None:
        cmd += [
            # Original aspect ratio, both edges capped, never upscaled, even sides;
            # JPEG with yuvj420p, which is what mjpeg accepts.
            "-vf",
            COVER_FILTER,
            "-codec:v",
            "mjpeg",
            "-q:v",
            "3",
            "-metadata:s:v",
            "title=Cover",
            "-metadata:s:v",
            "comment=Cover (front)",
            "-disposition:v",
            "attached_pic",
        ]
    cmd += [
        "-id3v2_version",
        "3",
        "-metadata",
        f"comment={sanitize_tag_value(COMMENT_TEXT)}",
    ]
    tempo = bpm_to_int(bpm)
    if tempo is not None:
        cmd += ["-metadata", f"TBPM={tempo}"]
    return [*cmd, "-f", "mp3", str(output)]


def read_bpm_tag(mp3_path: Path) -> int | None:
    """Return the TBPM already stored in *mp3_path*, or ``None``."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags",
        "-of",
        "json",
        str(mp3_path),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, timeout=_TAG_TIMEOUT_SECONDS, check=True)
        tags = (json.loads(completed.stdout or b"{}").get("format") or {}).get("tags") or {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("Could not read tags of %s: %s", mp3_path.name, exc)
        return None
    for key, value in tags.items():
        if str(key).upper() == "TBPM":
            try:
                return int(str(value).strip())
            except ValueError:
                return None
    return None


def _bpm_rewrite_plan(mp3_path: Path, bpm: Any) -> tuple[int, Path, list[str]] | None:
    """Resolve the rewrite preconditions shared by both rewrite variants.

    Returns ``(tempo, temp_path, cmd)`` when a rewrite is due, or ``None`` when
    there is no usable tempo, the file is missing, or the tag already matches.
    """
    tempo = bpm_to_int(bpm)
    if tempo is None or not mp3_path.is_file():
        return None
    if read_bpm_tag(mp3_path) == tempo:
        return None

    temp_path = mp3_path.with_name(f"{mp3_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(mp3_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "0",
        "-id3v2_version",
        "3",
        "-write_id3v2",
        "1",
        "-metadata",
        f"TBPM={tempo}",
        "-f",
        "mp3",
        str(temp_path),
    ]
    return tempo, temp_path, cmd


def _finalize_bpm_rewrite(
    mp3_path: Path,
    temp_path: Path,
    tempo: int,
    returncode: int,
    stderr: bytes,
) -> None:
    """Promote the rewritten temp file to the target or raise on a bad result."""
    if returncode != 0 or not temp_path.is_file() or temp_path.stat().st_size == 0:
        logger.error(
            "ffmpeg BPM tagging failed: %s",
            stderr.decode(errors="replace")[-500:],
        )
        raise RuntimeError("BPM tagging failed")
    temp_path.replace(mp3_path)
    logger.info("Wrote BPM tag %d into %s", tempo, mp3_path.name)


def ensure_bpm_tag(mp3_path: Path, bpm: Any) -> bool:
    """Write TBPM into an existing MP3 unless it already carries that tempo.

    Stream-copies everything (cover, comment, other tags stay), writes a temp
    file and replaces the original atomically. "No usable tempo" writes
    nothing. Returns True when the file was rewritten. Raises RuntimeError or
    OSError on failure, leaving the original untouched.

    Synchronous and blocking; prefer :func:`ensure_bpm_tag_async` from async
    code so cancellation can terminate the ffmpeg process.
    """
    plan = _bpm_rewrite_plan(mp3_path, bpm)
    if plan is None:
        return False
    tempo, temp_path, cmd = plan
    try:
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_TAG_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"BPM tagging failed: {exc}") from exc
        _finalize_bpm_rewrite(mp3_path, temp_path, tempo, completed.returncode, completed.stderr)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


async def _stop_ffmpeg_process(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a still-running ffmpeg process so cancellation orphans none."""
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    with suppress(ProcessLookupError):
        await asyncio.shield(proc.wait())


async def ensure_bpm_tag_async(mp3_path: Path, bpm: Any) -> bool:
    """Async counterpart of :func:`ensure_bpm_tag`.

    Runs ffmpeg as an asyncio subprocess so a cancelled await (client
    disconnect, shutdown) terminates the process through ``kill()`` instead of
    leaving it running to the 120 s timeout. The timeout still bounds a process
    that neither finishes nor is cancelled. Cancellation is re-raised after the
    process is reaped and the temp file removed. Behaviour and the returned
    value are otherwise identical to the synchronous variant.
    """
    # read_bpm_tag() shells out to ffprobe synchronously, so resolve the plan
    # off the event loop; running it inline would block for up to the timeout.
    plan = await asyncio.to_thread(_bpm_rewrite_plan, mp3_path, bpm)
    if plan is None:
        return False
    tempo, temp_path, cmd = plan
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"BPM tagging failed: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TAG_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            # Client disconnect / shutdown: reap the process, then let
            # cancellation propagate so the request is not silently completed.
            await _stop_ffmpeg_process(proc)
            raise
        except TimeoutError as exc:
            # A stuck ffmpeg is a tagging failure, not a cancellation: translate
            # it to RuntimeError so _ensure_mp3 serves the untagged MP3.
            await _stop_ffmpeg_process(proc)
            raise RuntimeError("BPM tagging timed out") from exc
        except BaseException:
            # Any other failure still must not orphan the ffmpeg process.
            await _stop_ffmpeg_process(proc)
            raise
        _finalize_bpm_rewrite(mp3_path, temp_path, tempo, proc.returncode or 0, stderr or b"")
    finally:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)
    return True
