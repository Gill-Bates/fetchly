# Essentia Build

The image compiles [Essentia](https://github.com/MTG/essentia) itself, for
`linux/amd64` and `linux/arm64`, from one commit. It is CPU-only and contains
only what BPM analysis uses.

## Revision: upstream master, once per release
Essentia follows upstream `master` ("latest is greatest"), like the other
rolling inputs. The release workflow resolves the head SHA once and passes it to
both architecture builds as `ESSENTIA_REF`, so amd64 and arm64 never get
different revisions. A new SHA also invalidates the cached Essentia stage. The
build log prints `essentia ref: <sha>`. The built checkout's SHA is stored in
`/venv/.essentia-commit`; CI fails if it differs from the resolved one and
records it in the toolchain manifest compared across architectures.

Without `ESSENTIA_REF` (a local build) the script resolves `master` itself; the
cached layer is then reused until you pass a new value or build with `--no-cache`.

Upstream's own `build_3rdparty_static_debian.sh` is not run. Since `33108c70`
(OnnxPredict) it builds onnxruntime with CUDA, which aborts on hosts without a
CUDA toolkit. Fetchly has no use for ONNX, TensorFlow or a GPU, so new upstream
third-party additions never enter the image on their own.

## What is built

`docker/build-essentia.sh` runs only the upstream scripts for Eigen (headers),
FFTW (float, SSE2 on amd64, NEON on arm64), an audio-only static FFmpeg and
libsamplerate. TagLib, libyaml and Chromaprint are left out, and waf drops the
algorithms that need them. The wheel is built in the `essentia` stage and only
the wheel reaches the runtime image, which has no compiler or headers.

## What the check protects

`docker/essentia_check.py` runs during the build and again in the finished image
on both architectures. It writes a 120 BPM click track, loads it with
`MonoLoader`, runs `RhythmExtractor2013(method="multifeature")` as
`app/audio/bpm.py` does, and asserts a plausible tempo. It also fails if
`_essentia.so` needs a shared library outside a short allowlist, or if a CUDA,
ONNX or TensorFlow library is loaded.

## Upgrading
There is nothing to bump: the next release builds the then-current `master`. If
that build fails, the cause is almost always a new mandatory dependency (waf
configure step) or unexpected linkage (`essentia_check.py`). Fix it in
`docker/build-essentia.sh`, or pass a known-good `ESSENTIA_REF` for a local
build. Do not widen the allowlist in `essentia_check.py` without a reason.
