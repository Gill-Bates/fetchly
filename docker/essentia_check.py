#!/usr/bin/env python3
#
# docker/essentia_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Functional check of the CPU-only essentia build, run on the target architecture.

Mirrors app/audio/bpm.py (MonoLoader, then RhythmExtractor2013 "multifeature")
and fails if the compiled extension links any GPU, ONNX or TensorFlow code.
"""

from __future__ import annotations

import math
import struct
import sys
import tempfile
import wave
from importlib import metadata
from pathlib import Path

SAMPLE_RATE = 44100
EXPECTED_BPM = 120.0
TOLERANCE_BPM = 2.0
DURATION_S = 20.0

# Shared libraries _essentia.so may need. Everything else (fftw, ffmpeg, samplerate)
# must be linked statically, so a new entry is a deliberate decision, not drift.
ALLOWED_NEEDED_PREFIXES = (
    "libc.so",
    "libm.so",
    "libstdc++.so",
    "libgcc_s.so",
    "libpthread.so",
    "libdl.so",
    "librt.so",
    "ld-linux",
    "libpython",
)
FORBIDDEN_MARKERS = ("cuda", "cudnn", "cublas", "nvidia", "onnx", "tensorflow", "gaia", "chromaprint", "taglib")

_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5


def needed_libraries(path: Path) -> list[str]:
    """Return the DT_NEEDED entries of a little-endian ELF64 file (x86-64 and aarch64)."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError(f"{path} is not a little-endian ELF64 file")
    (phoff,) = struct.unpack_from("<Q", data, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    loads: list[tuple[int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    for index in range(phnum):
        entry = struct.unpack_from("<IIQQQQ", data, phoff + index * phentsize)
        p_type, p_offset, p_vaddr, p_filesz = entry[0], entry[2], entry[3], entry[5]
        if p_type == _PT_LOAD:
            loads.append((p_vaddr, p_filesz, p_offset))
        elif p_type == _PT_DYNAMIC:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return []

    def to_offset(address: int) -> int:
        for vaddr, filesz, offset in loads:
            if vaddr <= address < vaddr + filesz:
                return address - vaddr + offset
        raise ValueError(f"address {address:#x} is outside every PT_LOAD segment")

    names: list[int] = []
    strtab = 0
    start, size = dynamic
    for position in range(start, start + size, 16):
        tag, value = struct.unpack_from("<qQ", data, position)
        if tag == _DT_NULL:
            break
        if tag == _DT_NEEDED:
            names.append(value)
        elif tag == _DT_STRTAB:
            strtab = to_offset(value)
    result = []
    for name in names:
        end = data.index(b"\0", strtab + name)
        result.append(data[strtab + name : end].decode())
    return result


def write_click_track(path: Path) -> None:
    """One decaying 1 kHz burst per beat at EXPECTED_BPM, the simplest tempo signal."""
    period = 60.0 / EXPECTED_BPM
    frames = bytearray()
    for n in range(int(SAMPLE_RATE * DURATION_S)):
        t = n / SAMPLE_RATE
        sample = math.sin(2.0 * math.pi * 1000.0 * t) * math.exp(-40.0 * (t % period)) * 0.8
        frames += struct.pack("<h", int(sample * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


def check_linkage() -> list[str]:
    import essentia

    package = Path(essentia.__file__).resolve().parent
    failures = []
    extensions = sorted(package.glob("_essentia*.so"))
    if not extensions:
        return [f"no _essentia*.so in {package}"]
    for extension in extensions:
        needed = needed_libraries(extension)
        print(f"{extension.name} needs: {', '.join(needed)}")
        failures.extend(
            f"{extension.name} links unexpected shared library {name}"
            for name in needed
            if not name.startswith(ALLOWED_NEEDED_PREFIXES)
        )
    failures.extend(
        f"unexpected shared library shipped in the wheel: {shared.relative_to(package)}"
        for shared in sorted(package.rglob("*.so*"))
        if not shared.name.startswith("_essentia")
    )
    return failures


def check_loaded_libraries() -> list[str]:
    """After a real analysis, no GPU/ONNX/TensorFlow library may be mapped into the process."""
    mapped = Path("/proc/self/maps").read_text().lower()
    return [f"a library matching '{marker}' is loaded at runtime" for marker in FORBIDDEN_MARKERS if marker in mapped]


def check_bpm() -> list[str]:
    from essentia.standard import MonoLoader, RhythmExtractor2013

    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        click = Path(tmp) / "click.wav"
        write_click_track(click)
        audio = MonoLoader(filename=str(click))()
    bpm, beats, confidence, _, _ = RhythmExtractor2013(method="multifeature")(audio)
    bpm = float(bpm)
    print(
        f"samples loaded: {len(audio)}, BPM {bpm:.3f} (expected {EXPECTED_BPM} +/- {TOLERANCE_BPM}), "
        f"beats {len(beats)}, confidence {float(confidence):.3f}"
    )
    if len(audio) == 0:
        failures.append("MonoLoader returned no samples")
    if not math.isfinite(bpm) or abs(bpm - EXPECTED_BPM) > TOLERANCE_BPM:
        failures.append(f"BPM {bpm!r} is not within {TOLERANCE_BPM} of {EXPECTED_BPM}")
    if len(beats) < int(DURATION_S / (60.0 / EXPECTED_BPM)) - 4:
        failures.append(f"only {len(beats)} beats found in {DURATION_S:.0f}s")
    return failures


def main() -> int:
    import essentia.standard  # noqa: F401  (import check)

    print(f"essentia {metadata.version('essentia')}")
    failures = check_linkage() + check_bpm() + check_loaded_libraries()
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("essentia check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
