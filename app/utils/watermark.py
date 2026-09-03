#!/usr/bin/env python3
#
# app/utils/watermark.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Burned-in fetchly watermark for downloaded videos.

A watermark can only be burned in by re-encoding, so the expensive part is the
encoder - not the drawing. Everything here is built around keeping the
per-frame cost at zero:

* The logo ships as a pre-rendered PNG (``app/static/img/fetchly_watermark.png``,
  rasterized from ``fetchly_logo.svg``). The runtime ffmpeg is a static upstream
  build and is not assumed to carry an SVG decoder.
* Logo, drop shadow and the instance hostname are composited into **one** RGBA
  badge, sized in exact output pixels, by a single ffmpeg call. The result is
  cached on disk, keyed by hostname and size, so a given deployment renders it
  once and every later download reuses the file.
* The transcode then only does ``overlay`` of a still image. ffmpeg decodes and
  scales that image once and reuses the frame, so the added work per video frame
  is one alpha blend over a few thousand pixels in the corner.

Where an encode already happens (the 480p/720p transcode) the watermark
therefore costs nothing measurable; only ``max`` quality, which is otherwise a
pure download+remux, pays for an encoder pass.

The hostname line is drawn only when a "Public hostname" is configured in
Settings - without one there is no address worth stamping on the video, so just
the logo goes on.
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

logger = logging.getLogger(__name__)

__all__ = [
    "VideoWatermark",
    "badge_geometry",
    "build_watermark",
    "video_filter_args",
]

# Pre-rendered logo artwork. 557x160 RGBA, white on transparent.
_LOGO_PNG: Final[Path] = Path(__file__).parent.parent / "static" / "img" / "fetchly_watermark.png"

# The UI uses Roboto Flex as WOFF2, but static ffmpeg builds do not always
# enable FreeType's WOFF2 decoder. Use the same font in its TrueType container
# for the hostname line so the Docker image needs neither fontconfig nor a
# fallback system font. drawtext rasterizes its default instance (wght 400,
# wdth 100, opsz 14) because it does not request a variation.
_FONT_TTF: Final[Path] = Path(__file__).parent.parent / "static" / "fonts" / "roboto-flex.ttf"

# Cached badges live outside the per-job directories: housekeeping only sweeps
# UUID-named folders (app/utils/housekeeping.py::cleanup_orphaned_directories),
# so this one is left alone.
_CACHE_DIRNAME: Final[str] = "watermark-cache"

# Bumped whenever the badge layout changes, so upgraded installs re-render
# instead of serving a stale cache entry.
_BADGE_REVISION: Final[str] = "3"

# Logo aspect ratio, straight from the SVG viewBox.
_LOGO_ASPECT: Final[float] = 203.56738 / 58.475399

# Badge size as a fraction of the video width. The clamps only guard genuinely
# degenerate inputs - a thumbnail-sized source or an exotic beyond-8K one -
# so the badge stays at the same 12% of frame width from SD up through 8K
# instead of ballooning on small downloads or shrinking on large ones.
_WIDTH_FRACTION: Final[float] = 0.12
_MIN_LOGO_WIDTH: Final[int] = 32
_MAX_LOGO_WIDTH: Final[int] = 960
# Badge widths snap to this grid so the on-disk cache stays small.
_WIDTH_STEP: Final[int] = 8

# Inset from the bottom-right corner, as a fraction of the video height.
_MARGIN_FRACTION: Final[float] = 0.025
_MIN_MARGIN: Final[int] = 8

# The logo's own drop shadow: a blurred, blackened copy of its silhouette,
# offset down-right. Both scale with the logo so it stays "light" (subtle)
# whether the badge is a small 480p corner mark or a 4K one; the diagonal
# offset and the blur are proportioned relative to each other so the shadow
# reads as soft rather than a second outline.
_SHADOW_OFFSET_FACTOR: Final[float] = 0.09
_MIN_SHADOW_OFFSET: Final[int] = 2
_SHADOW_BLUR_FACTOR: Final[float] = 0.055
_MIN_SHADOW_BLUR: Final[float] = 1.0
_SHADOW_ALPHA: Final[float] = 0.55

# Mean glyph advance of Roboto Flex (default instance: wght 400, wdth 100,
# opsz 14) in em, used to pick a font size that keeps the hostname from
# growing much wider than the logo. Deliberately generous: overestimating
# only leaves transparent padding, underestimating would clip.
_MEAN_ADVANCE_EM: Final[float] = 0.52
_MAX_TEXT_WIDTH_FACTOR: Final[float] = 1.6
_MIN_FONT_SIZE: Final[int] = 8

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
    """Escape a literal for use as a drawtext ``text=`` value.

    Hostnames are already restricted to letters, digits, dots, hyphens and (for
    IPv6) colons by ``normalize_public_hostname``; the colon is the one
    character that would otherwise end the filter argument. The rest is
    defensive - a filtergraph built from stored settings should never be able to
    grow an extra option.
    """
    for char in ("\\", ":", "'", "%", ",", "[", "]", ";"):
        value = value.replace(char, f"\\{char}")
    return value


def badge_geometry(video_width: int, video_height: int, text: str) -> _Geometry:
    """Lay out the badge for a given output size.

    Pure arithmetic, no I/O - the cache key and the ffmpeg filtergraph are both
    derived from the result.
    """
    # Quantized to _WIDTH_STEP: source resolutions are arbitrary, and an exact
    # fit would give the cache a badge per distinct video width. Snapping keeps
    # it to a few dozen entries at a size difference nobody can see.
    scaled = round(video_width * _WIDTH_FRACTION / _WIDTH_STEP) * _WIDTH_STEP
    logo_width = min(_MAX_LOGO_WIDTH, max(_MIN_LOGO_WIDTH, scaled))
    logo_height = max(1, round(logo_width / _LOGO_ASPECT))
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
    text_height = round(font_size * 1.45)

    canvas_width = max(logo_width, text_width) + shadow_offset
    return _Geometry(
        canvas_width=canvas_width,
        canvas_height=logo_height + gap + text_height + shadow_offset,
        logo_width=logo_width,
        logo_height=logo_height,
        # Logo and hostname are both flush right, matching the corner they sit in.
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

    ``font_file`` is resolved once by the caller rather than looked up here:
    build_watermark() already had to check it to decide whether to draw *text*
    at all, and a second _font_file() call could disagree with the first (and
    would log the "font missing" warning twice), emitting an empty
    ``fontfile=`` that ffmpeg rejects.
    """
    offset = geometry.shadow_offset
    chain = [
        (f"[1:v]scale={geometry.logo_width}:{geometry.logo_height}:flags=lanczos,"
        "format=rgba,split=2[lg][sh]"),
        # The shadow is the logo's own alpha, blacked out and softened with a
        # Gaussian blur - a plain offset silhouette reads as a second outline
        # rather than a shadow, the blur is what makes it look "light" instead
        # of stamped on.
        (f"[sh]colorchannelmixer=rr=0:gg=0:bb=0:aa={_SHADOW_ALPHA},"
        f"gblur=sigma={geometry.shadow_blur:.2f}:steps=1[shadow]"),
        "[lg]colorchannelmixer=aa=0.92[logo]",
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
            "fontcolor=white@0.92:"
            "shadowcolor=black@0.5:"
            f"shadowx={text_offset}:shadowy={text_offset}:"
            f"x=w-tw-{offset}:y={geometry.text_y}"
        )
    return ";".join(chain)


def _cache_key(hostname: str, geometry: _Geometry) -> str:
    raw = f"{_BADGE_REVISION}|{hostname}|{geometry.canvas_width}x{geometry.canvas_height}|{geometry.font_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _render_badge(target: Path, geometry: _Geometry, text: str, font_file: str) -> None:
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
        str(_LOGO_PNG),
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
    if not _LOGO_PNG.is_file():
        logger.warning("Watermark artwork missing at %s; skipping watermark", _LOGO_PNG)
        return None

    # Resolved once and threaded through to the filtergraph: a second lookup
    # could disagree with this one and yield an empty fontfile= (see
    # _badge_filtergraph).
    font_file = _font_file()

    try:
        text = normalize_public_hostname(hostname) if font_file else ""
    except ValueError:
        # A hostname that fails validation here was stored before the rules
        # tightened; the logo alone is still a valid watermark.
        logger.warning("Stored public hostname is not usable for the watermark; drawing the logo only")
        text = ""

    geometry = badge_geometry(video_width, video_height, text)
    directory = cache_dir if cache_dir is not None else get_data_dir() / _CACHE_DIRNAME
    badge = directory / f"{_cache_key(text, geometry)}.png"

    if not badge.is_file():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _render_badge(badge, geometry, text, font_file)
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
    """ffmpeg arguments that apply ``scale`` and/or the watermark to input 0.

    Without a watermark this is the plain ``-vf`` the transcode always used.
    With one, the badge comes in as input 1 and the graph has to be explicit
    about stream mapping - ffmpeg's automatic selection would otherwise pick the
    still image as the output video stream.
    """
    if watermark is None:
        return ["-vf", scale] if scale else []

    stages = []
    if scale:
        stages.append(f"[0:v]{scale}[base]")
        main = "[base]"
    else:
        main = "[0:v]"
    # format=yuv420p after the blend: the badge is RGBA, and the alpha would
    # otherwise reach the encoder and produce a file some players reject.
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
