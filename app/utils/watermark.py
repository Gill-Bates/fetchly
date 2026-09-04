#!/usr/bin/env python3
#
# app/utils/watermark.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Burned-in fetchly watermark for downloaded videos.

Built to keep the per-frame cost at zero:

* Logo ships as a pre-rendered PNG (static ffmpeg has no SVG decoder).
* Logo, drop shadow and hostname are composited into one RGBA badge, sized in
  output pixels, by a single ffmpeg call, then cached on disk keyed by
  hostname and size.
* The transcode then only ``overlay``s that still image - one alpha blend per
  frame over a corner.

So on the capped qualities (which already encode) the watermark is free; only
``max`` pays for an encoder pass. The hostname line is drawn only when a
"Public hostname" is configured.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .fs import get_data_dir
from .public_url import normalize_public_hostname
from .watermark_logo import custom_logo_path, logo_status

logger = logging.getLogger(__name__)

__all__ = [
    "VideoWatermark",
    "badge_geometry",
    "build_watermark",
    "video_filter_args",
]

# Pre-rendered logo artwork. 557x160 RGBA, white on transparent. Replaced by a
# user-supplied logo when one is stored (see watermark_logo.py); the bundled
# file is never overwritten, so removing the custom one restores it.
_LOGO_PNG: Final[Path] = Path(__file__).parent.parent / "static" / "img" / "fetchly_watermark.png"

# Roboto Flex as TrueType (not the UI's WOFF2, which static ffmpeg's FreeType
# may not decode), so the image needs no fontconfig or system font. drawtext
# rasterizes the default instance (wght 400, wdth 100, opsz 14).
_FONT_TTF: Final[Path] = Path(__file__).parent.parent / "static" / "fonts" / "roboto-flex.ttf"

# Cached badges live outside the per-job directories: housekeeping only sweeps
# UUID-named folders (app/utils/housekeeping.py::cleanup_orphaned_directories),
# so this one is left alone.
_CACHE_DIRNAME: Final[str] = "watermark-cache"

# Bumped whenever the badge layout changes, so upgraded installs re-render
# instead of serving a stale cache entry.
_BADGE_REVISION: Final[str] = "4"

# Aspect ratio of the bundled logo, straight from the SVG viewBox. A custom
# logo brings its own, measured from the stored PNG.
_LOGO_ASPECT: Final[float] = 203.56738 / 58.475399

# Badge width as a fraction of the video width; the clamps only guard
# degenerate inputs (thumbnail-sized or beyond-8K).
_WIDTH_FRACTION: Final[float] = 0.12
_MIN_LOGO_WIDTH: Final[int] = 32
_MAX_LOGO_WIDTH: Final[int] = 960
# Badge widths snap to this grid so the on-disk cache stays small.
_WIDTH_STEP: Final[int] = 8

# Inset from the bottom-right corner, as a fraction of the video height.
_MARGIN_FRACTION: Final[float] = 0.025
_MIN_MARGIN: Final[int] = 8

# The logo's drop shadow: a blurred, blackened, offset copy of its silhouette.
# All three scale with the logo so it stays subtle at any size.
_SHADOW_OFFSET_FACTOR: Final[float] = 0.09
_MIN_SHADOW_OFFSET: Final[int] = 2
_SHADOW_BLUR_FACTOR: Final[float] = 0.055
_MIN_SHADOW_BLUR: Final[float] = 1.0
_SHADOW_ALPHA: Final[float] = 0.35  # faint: a cue, not a second outline

# The logo is translucent like a broadcaster's on-screen bug; the hostname
# line stays fully legible (see fontcolor below).
_LOGO_ALPHA: Final[float] = 0.55

# Mean glyph advance of Roboto Flex (em), used to size the hostname so it does
# not grow much wider than the logo. Generous on purpose: overestimating only
# pads, underestimating clips.
_MEAN_ADVANCE_EM: Final[float] = 0.52
_MAX_TEXT_WIDTH_FACTOR: Final[float] = 1.6
_MIN_FONT_SIZE: Final[int] = 8

# Rendered text ink (glyphs + drawtext border/shadow) measures ~1.0-1.25x the
# font size, worst at small sizes where the 1px border does not shrink. 1.3x
# covers it so the badge's bottom margin matches its right margin.
_TEXT_INK_FACTOR: Final[float] = 1.3

_BADGE_TIMEOUT_SECONDS: Final[int] = 30


@dataclass(frozen=True, slots=True)
class VideoWatermark:
    """A rendered badge plus where it goes in the output frame."""

    path: Path
    width: int
    height: int
    margin: int


@dataclass(frozen=True, slots=True)
class _Geometry:
    """Pixel layout of the badge, derived from the output video size."""

    canvas_width: int
    canvas_height: int
    logo_width: int
    logo_height: int
    logo_x: int
    font_size: int
    text_y: int
    shadow_offset: int
    shadow_blur: float
    text_shadow_offset: int
    margin: int


def _font_file() -> str:
    """Path of the bundled Roboto Flex, or ``""`` when it is missing."""
    if _FONT_TTF.is_file():
        return str(_FONT_TTF)
    logger.warning(
        "Watermark font missing at %s; the instance hostname will be omitted.", _FONT_TTF
    )
    return ""


def _escape_drawtext(value: str) -> str:
    """Escape a literal for a drawtext ``text=`` value.

    Hostnames are already restricted by normalize_public_hostname (the colon is
    the one meaningful case); the rest is defensive.
    """
    for char in ("\\", ":", "'", "%", ",", "[", "]", ";"):
        value = value.replace(char, f"\\{char}")
    return value


def badge_geometry(
    video_width: int,
    video_height: int,
    text: str,
    logo_aspect: float = _LOGO_ASPECT,
) -> _Geometry:
    """Lay out the badge for a given output size.

    Pure arithmetic, no I/O - the cache key and the ffmpeg filtergraph are both
    derived from the result. ``logo_aspect`` is width/height of the artwork, so
    a custom logo keeps its proportions instead of being squeezed into the
    bundled one's.
    """
    # Quantized to _WIDTH_STEP so the cache holds a few dozen sizes, not one
    # per distinct video width.
    scaled = round(video_width * _WIDTH_FRACTION / _WIDTH_STEP) * _WIDTH_STEP
    logo_width = min(_MAX_LOGO_WIDTH, max(_MIN_LOGO_WIDTH, scaled))
    logo_height = max(1, round(logo_width / max(0.01, logo_aspect)))
    shadow_offset = max(_MIN_SHADOW_OFFSET, round(logo_height * _SHADOW_OFFSET_FACTOR))
    shadow_blur = max(_MIN_SHADOW_BLUR, logo_height * _SHADOW_BLUR_FACTOR)
    text_shadow_offset = max(1, round(shadow_offset / 2))
    margin = max(_MIN_MARGIN, round(video_height * _MARGIN_FRACTION))

    if not text:
        return _Geometry(
            canvas_width=logo_width + shadow_offset,
            canvas_height=logo_height + shadow_offset,
            logo_width=logo_width,
            logo_height=logo_height,
            logo_x=0,
            font_size=0,
            text_y=0,
            shadow_offset=shadow_offset,
            shadow_blur=shadow_blur,
            text_shadow_offset=text_shadow_offset,
            margin=margin,
        )

    font_size = max(_MIN_FONT_SIZE, round(logo_height * 0.40))
    # Shrink the type rather than let a long hostname stretch the badge.
    width_budget = logo_width * _MAX_TEXT_WIDTH_FACTOR
    fitted = width_budget / (_MEAN_ADVANCE_EM * max(1, len(text)))
    font_size = max(_MIN_FONT_SIZE, min(font_size, round(fitted)))

    text_width = round(_MEAN_ADVANCE_EM * font_size * len(text))
    gap = max(2, round(logo_height * 0.15))
    text_height = round(font_size * _TEXT_INK_FACTOR)

    canvas_width = max(logo_width, text_width) + shadow_offset
    return _Geometry(
        canvas_width=canvas_width,
        # No trailing "+ shadow_offset": the text row is drawn last and its ink
        # (border/shadow included) is already in text_height. Adding the logo's
        # shadow_offset here made the bottom margin exceed the right one.
        canvas_height=logo_height + gap + text_height,
        logo_width=logo_width,
        logo_height=logo_height,
        # Logo and hostname both flush right, matching their corner.
        logo_x=canvas_width - shadow_offset - logo_width,
        font_size=font_size,
        text_y=logo_height + gap,
        shadow_offset=shadow_offset,
        shadow_blur=shadow_blur,
        text_shadow_offset=text_shadow_offset,
        margin=margin,
    )


def _badge_filtergraph(geometry: _Geometry, text: str, font_file: str) -> str:
    """Composite logo, drop shadow and hostname onto a transparent canvas.

    ``font_file`` is passed in, not looked up here: a second _font_file() call
    could disagree and emit an empty ``fontfile=`` that ffmpeg rejects.
    """
    offset = geometry.shadow_offset
    chain = [
        (f"[1:v]scale={geometry.logo_width}:{geometry.logo_height}:flags=lanczos,"
        "format=rgba,split=2[lg][sh]"),
        # Shadow = the logo alpha, blacked out and Gaussian-blurred so it reads
        # as soft, not as a second outline.
        (f"[sh]colorchannelmixer=rr=0:gg=0:bb=0:aa={_SHADOW_ALPHA},"
        f"gblur=sigma={geometry.shadow_blur:.2f}:steps=1[shadow]"),
        f"[lg]colorchannelmixer=aa={_LOGO_ALPHA}[logo]",
        f"[0:v][shadow]overlay={geometry.logo_x + offset}:{offset}[bg]",
        f"[bg][logo]overlay={geometry.logo_x}:0" + ("[out]" if text else ""),
    ]
    if text:
        text_offset = geometry.text_shadow_offset
        chain.append(
            "[out]drawtext="
            f"fontfile={font_file}:"
            f"text='{_escape_drawtext(text)}':"
            f"fontsize={geometry.font_size}:"
            # Opaque with a dark border: the hostname is information to read
            # off, so full contrast against any footage (unlike the logo).
            "fontcolor=white:"
            "borderw=1:bordercolor=black@0.6:"
            "shadowcolor=black@0.6:"
            f"shadowx={text_offset}:shadowy={text_offset}:"
            f"x=w-tw-{offset}:y={geometry.text_y}"
        )
    return ";".join(chain)


@dataclass(frozen=True, slots=True)
class _Logo:
    """The artwork the badge is built from."""

    path: Path
    aspect: float
    # Distinguishes one logo from another in the badge cache key. Empty for the
    # bundled artwork, which only changes when the application does.
    fingerprint: str


def _resolve_logo() -> _Logo | None:
    """The custom logo when one is stored and readable, else the bundled one.

    A custom logo that has gone missing or unreadable since it was uploaded
    falls back rather than failing: a watermark is cosmetic, and the bundled
    artwork is always there.
    """
    status = logo_status()
    if status.custom and status.height > 0:
        return _Logo(
            path=custom_logo_path(),
            aspect=status.width / status.height,
            fingerprint=status.fingerprint,
        )

    if not _LOGO_PNG.is_file():
        logger.warning("Watermark artwork missing at %s; skipping watermark", _LOGO_PNG)
        return None
    return _Logo(path=_LOGO_PNG, aspect=_LOGO_ASPECT, fingerprint="")


def _cache_key(hostname: str, geometry: _Geometry, logo_fingerprint: str) -> str:
    # The fingerprint is part of the key, not a reason to sweep the cache:
    # swapping logos back and forth reuses both sets of badges.
    raw = (
        f"{_BADGE_REVISION}|{hostname}|{geometry.canvas_width}x{geometry.canvas_height}"
        f"|{geometry.font_size}|{logo_fingerprint}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _render_badge(target: Path, geometry: _Geometry, text: str, font_file: str, logo: Path) -> None:
    """Render the badge PNG to ``target`` via a temporary file."""
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black@0.0:s={geometry.canvas_width}x{geometry.canvas_height}:d=1,format=rgba",
        "-i",
        str(logo),
        "-filter_complex",
        _badge_filtergraph(geometry, text, font_file),
        "-frames:v",
        "1",
        "-pix_fmt",
        "rgba",
    ]
    # Written under a unique name and renamed into place, so two workers racing
    # on the same badge cannot hand ffmpeg a half-written file to overlay.
    tmp_target = target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}.tmp.png")
    cmd.append(str(tmp_target))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=_BADGE_TIMEOUT_SECONDS)
        tmp_target.replace(target)
    finally:
        tmp_target.unlink(missing_ok=True)


def build_watermark(
    *,
    video_width: int,
    video_height: int,
    hostname: str,
    cache_dir: Path | None = None,
) -> VideoWatermark | None:
    """Return the badge to overlay on a video of this size, or ``None``.

    ``None`` means "carry on without a watermark": the artwork is missing, or
    ffmpeg could not render the badge. Neither is worth failing a download over.
    """
    if video_width <= 0 or video_height <= 0:
        return None

    logo = _resolve_logo()
    if logo is None:
        return None

    # Resolved once and threaded through (see _badge_filtergraph).
    font_file = _font_file()

    try:
        text = normalize_public_hostname(hostname) if font_file else ""
    except ValueError:
        # Hostname stored before the rules tightened; the logo alone still works.
        logger.warning("Stored public hostname is not usable for the watermark; drawing the logo only")
        text = ""

    geometry = badge_geometry(video_width, video_height, text, logo.aspect)
    directory = cache_dir if cache_dir is not None else get_data_dir() / _CACHE_DIRNAME
    badge = directory / f"{_cache_key(text, geometry, logo.fingerprint)}.png"

    if not badge.is_file():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _render_badge(badge, geometry, text, font_file, logo.path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            logger.warning(
                "Could not render the video watermark: %s %s",
                exc,
                stderr.decode("utf-8", "replace").strip()[:400],
            )
            return None

    return VideoWatermark(
        path=badge,
        width=geometry.canvas_width,
        height=geometry.canvas_height,
        margin=geometry.margin,
    )


def video_filter_args(watermark: VideoWatermark | None, *, scale: str = "") -> list[str]:
    """ffmpeg args that apply ``scale`` and/or the watermark to input 0.

    With a watermark the badge is input 1 and the graph maps streams
    explicitly, or ffmpeg would pick the still image as the output video.
    """
    if watermark is None:
        return ["-vf", scale] if scale else []

    stages = []
    if scale:
        stages.append(f"[0:v]{scale}[base]")
        main = "[base]"
    else:
        main = "[0:v]"
    # format=yuv420p after the blend: the badge's RGBA alpha would otherwise
    # reach the encoder and produce a file some players reject.
    stages.append(
        f"{main}[1:v]overlay=W-w-{watermark.margin}:H-h-{watermark.margin}:format=auto,"
        "format=yuv420p[v]"
    )
    return [
        "-filter_complex",
        ";".join(stages),
        "-map",
        "[v]",
        # Optional: a source without an audio track must not fail the map.
        "-map",
        "0:a?",
    ]
