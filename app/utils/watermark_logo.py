#!/usr/bin/env python3
#
# app/utils/watermark_logo.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Storage and validation for a user-supplied watermark logo.

The Settings page takes an SVG or a PNG, and what arrives here is always a PNG:
a dropped PNG is uploaded untouched, and a dropped SVG is rasterized on a
canvas in the browser and only the result is sent. Two reasons for that
asymmetry, both of them about what the server has to trust.

* **No SVG renderer on the server.** ffmpeg's librsvg decoder is present in
  some builds (Debian's) and absent in others - the static BtbN builds the
  Docker image ships from. Rasterizing here would work in development and fail
  in the image, which is the worst place to find out.
* **No SVG stored or served.** An SVG is a script-carrying document. Serving
  one back from our own origin would be a stored-XSS primitive; keeping only
  the flattened PNG removes the whole class. The browser's own rasterization
  is safe for the same reason: an SVG drawn through ``<img>`` runs in secure
  static mode - no scripts, no external references.

A PNG needs none of that: it is already flat pixels, so it travels as the user
exported it.

So the client-side checks are convenience, and everything in this module is the
actual boundary: it re-derives every property from the bytes it was handed,
using ffprobe/ffmpeg, and stores the file only once they all hold.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .fs import get_data_dir

logger = logging.getLogger(__name__)

__all__ = [
    "LogoRejectedError",
    "LogoStatus",
    "custom_logo_path",
    "logo_status",
    "remove_custom_logo",
    "store_custom_logo",
]

# Its own subdirectory of the data volume, next to "cookies" and the badge
# cache: one named place per kind of thing an operator might mount, back up or
# inspect. Housekeeping only sweeps UUID-named job directories, so a plain name
# here is left alone.
_LOGO_DIRNAME: Final[str] = "logo"
_LOGO_FILENAME: Final[str] = "watermark-logo.png"

# 2 MiB is generous for logo artwork (the bundled one is ~10 KiB) and small
# enough that a rejected upload costs nothing.
MAX_LOGO_BYTES: Final[int] = 2 * 1024 * 1024

# Below the minimum the badge would be upscaled on any normal video; above the
# maximum there is nothing left to gain, since the badge is never drawn wider
# than 960px (watermark.py::_MAX_LOGO_WIDTH).
MIN_LOGO_PIXELS: Final[int] = 32
MAX_LOGO_PIXELS: Final[int] = 4096

# A badge sits in a corner. Wildly elongated artwork either shrinks to an
# illegible sliver or takes over the frame, so both extremes are refused.
MIN_LOGO_ASPECT: Final[float] = 0.1
MAX_LOGO_ASPECT: Final[float] = 20.0

# Pixel formats a PNG can decode to. Only the ones carrying an alpha channel
# are usable: a fully opaque logo would sit on the video as a solid rectangle.
# A palette PNG (pal8) is in here because its palette may carry transparency;
# whether it actually does is what _has_visible_transparency() then decides.
_ALPHA_PIXEL_FORMATS: Final[frozenset[str]] = frozenset({
    "rgba", "bgra", "argb", "abgr", "ya8", "ya16be", "ya16le",
    "rgba64be", "rgba64le", "pal8",
})

_PROBE_TIMEOUT_SECONDS: Final[int] = 20


class LogoRejectedError(Exception):
    """The uploaded file cannot be used as a watermark.

    The message is written for the person who dropped the file: it says what
    is wrong with their artwork, never what the server did to find out.
    """


@dataclass(frozen=True, slots=True)
class LogoStatus:
    """What the Settings page needs to render the drop zone."""

    custom: bool
    width: int = 0
    height: int = 0
    bytes: int = 0
    fingerprint: str = ""


def custom_logo_path() -> Path:
    return get_data_dir() / _LOGO_DIRNAME / _LOGO_FILENAME


def _probe_png(path: Path) -> tuple[int, int, str]:
    """``(width, height, pix_fmt)`` of a PNG, or raise LogoRejectedError."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,pix_fmt",
        "-of", "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            cmd, check=True, capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS
        )
        stream = (json.loads(completed.stdout or "{}").get("streams") or [{}])[0]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.info("Rejected watermark logo: not decodable (%s)", exc)
        raise LogoRejectedError("That file is not a readable image.") from exc

    if stream.get("codec_name") != "png":
        raise LogoRejectedError("That file is not a PNG image.")

    # ffprobe reports a stream for a file it only *guessed* was a PNG (it logs
    # the bad signature and exits 0), and gives it a zero size. So the size is
    # the check that a decoder actually got somewhere, not just a formality.
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise LogoRejectedError("That file is not a readable image.")

    return width, height, str(stream.get("pix_fmt") or "")


def _has_visible_transparency(path: Path) -> bool:
    """Whether any pixel is not fully opaque.

    A logo without transparency is not artwork on the video, it is a box over
    it - so this is the one content check that decides usability rather than
    validity. Read straight off the alpha plane: fully opaque means the
    minimum alpha value is the maximum of the range.
    """
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-i", str(path),
        # "format=rgba" first, so this holds for every PNG a user can export:
        # alphaextract has no plane to take from a palette image at all, and on
        # a 16-bit one it reports alpha on a 0-65535 scale that the comparison
        # below would read backwards. Both arrive here as 8-bit alpha.
        #
        # "file=-" puts the reading on stdout: metadata=print otherwise logs at
        # info level, which "-v error" swallows - and a silent probe would read
        # as "no transparency found" for every image.
        "-vf", "format=rgba,alphaextract,signalstats,metadata=print:key=lavfi.signalstats.YMIN:file=-",
        "-f", "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            cmd, check=True, capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS, text=True
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # An image whose alpha cannot be read is not worth failing an upload
        # over; the size and format checks already did the real work.
        logger.warning("Could not read the alpha channel of an uploaded logo: %s", exc)
        return True

    for line in (completed.stdout or "").splitlines():
        if "lavfi.signalstats.YMIN" in line:
            try:
                return float(line.rsplit("=", 1)[1]) < 255.0
            except (IndexError, ValueError):
                return True
    return True


def validate_logo_bytes(data: bytes, target: Path) -> LogoStatus:
    """Write ``data`` to ``target`` only if it is a usable watermark logo.

    Every check re-derives its answer from the bytes, never from what the
    client claimed. The file is written to a temporary name first, so a
    rejected upload cannot leave a half-valid logo behind.
    """
    if not data:
        raise LogoRejectedError("That file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise LogoRejectedError(f"That image is larger than {MAX_LOGO_BYTES // (1024 * 1024)} MB.")

    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}.tmp.png")
    try:
        staged.write_bytes(data)

        width, height, pix_fmt = _probe_png(staged)
        if width < MIN_LOGO_PIXELS or height < MIN_LOGO_PIXELS:
            raise LogoRejectedError(
                f"That image is smaller than {MIN_LOGO_PIXELS}x{MIN_LOGO_PIXELS} pixels."
            )
        if width > MAX_LOGO_PIXELS or height > MAX_LOGO_PIXELS:
            raise LogoRejectedError(f"That image is larger than {MAX_LOGO_PIXELS} pixels on a side.")

        aspect = width / height
        if not MIN_LOGO_ASPECT <= aspect <= MAX_LOGO_ASPECT:
            raise LogoRejectedError("That image is too elongated to sit in a video corner.")

        if pix_fmt not in _ALPHA_PIXEL_FORMATS:
            raise LogoRejectedError(
                "That image has no transparency; it would cover the video with a solid box."
            )
        if not _has_visible_transparency(staged):
            raise LogoRejectedError(
                "Every pixel of that image is opaque; it would cover the video with a solid box."
            )

        staged.replace(target)
        return LogoStatus(
            custom=True,
            width=width,
            height=height,
            bytes=len(data),
            fingerprint=hashlib.sha256(data).hexdigest()[:16],
        )
    finally:
        staged.unlink(missing_ok=True)


def store_custom_logo(data: bytes) -> LogoStatus:
    """Validate and install an uploaded logo, replacing any previous one."""
    status = validate_logo_bytes(data, custom_logo_path())
    logger.info("Installed a custom watermark logo (%dx%d, %d bytes)", status.width, status.height, status.bytes)
    return status


def remove_custom_logo() -> bool:
    """Drop the custom logo and fall back to the bundled artwork."""
    path = custom_logo_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not remove the custom watermark logo: %s", exc)
        raise
    logger.info("Removed the custom watermark logo")
    return True


def logo_status() -> LogoStatus:
    """Describe the logo currently in use, bundled or custom."""
    path = custom_logo_path()
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, OSError):
        return LogoStatus(custom=False)

    try:
        width, height, _ = _probe_png(path)
    except LogoRejectedError:
        # Present but unreadable: report it as absent so the UI offers a fresh
        # upload rather than a broken preview. watermark.py ignores it too.
        logger.warning("The stored custom watermark logo is not readable; ignoring it")
        return LogoStatus(custom=False)

    return LogoStatus(
        custom=True,
        width=width,
        height=height,
        bytes=len(raw),
        fingerprint=hashlib.sha256(raw).hexdigest()[:16],
    )
