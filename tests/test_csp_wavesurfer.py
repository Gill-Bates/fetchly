#!/usr/bin/env python3
#
# tests/test_csp_wavesurfer.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Guards the CSP hash that lets WaveSurfer's shadow-DOM stylesheet load.

WaveSurfer builds a <style> element into the shadow root of the trim view.
Under `style-src 'self'` the browser blocks it, and the rules that position
the progress canvas on top of the waveform never apply - the canvas drops
into normal flow and renders as a second waveform below the first, with the
playback cursor collapsed to zero height. The policy therefore carries a
hash of exactly that stylesheet.

The hash covers the stylesheet's text, which interpolates the configured
waveform height. So it goes stale if either the vendored bundle or that
height changes - both are pinned here so the change fails loudly instead of
silently breaking the trim view again.
"""

import hashlib
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-csp-wavesurfer-secret")

from app.main import _WAVESURFER_STYLE_HASH, SecurityHeadersMiddleware

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLE = _REPO_ROOT / "app/static/vendor/wavesurfer/dist/wavesurfer.esm.js"
_TRIM_JS = _REPO_ROOT / "app/static/js/trim.js"

# sha256 of the vendored bundle the hash above was taken from.
_PINNED_BUNDLE_SHA256 = "2621439837525ed48935ed9939b082133aa8d84574d261104029680dcfb34d1d"
# The height WaveSurfer.create() is called with; it is interpolated into the
# stylesheet, so the hash is only valid for this value.
_PINNED_WAVE_HEIGHT = 100

_REFRESH_HINT = (
    "Open the trim view in a browser: the CSP error names the new hash. "
    "Put it in app/main.py::_WAVESURFER_STYLE_HASH and update the pin here."
)


class WaveSurferCspHashTests(unittest.TestCase):
    def test_policy_allows_the_shadow_stylesheet_without_unsafe_inline(self) -> None:
        directive = next(
            part for part in SecurityHeadersMiddleware._CSP.split("; ")
            if part.startswith("style-src")
        )
        self.assertIn(_WAVESURFER_STYLE_HASH, directive)
        # The hash exists precisely so the blanket keyword is not needed.
        self.assertNotIn("unsafe-inline", directive)

    def test_vendored_bundle_still_matches_the_hashed_stylesheet(self) -> None:
        digest = hashlib.sha256(_BUNDLE.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            _PINNED_BUNDLE_SHA256,
            f"WaveSurfer was updated, so its stylesheet - and the CSP hash - may have changed. {_REFRESH_HINT}",
        )

    def test_wave_height_still_matches_the_hashed_stylesheet(self) -> None:
        match = re.search(r"^\s*height:\s*(\d+),\s*$", _TRIM_JS.read_text(encoding="utf-8"), re.MULTILINE)
        assert match is not None, "No height option found in the WaveSurfer.create() call"
        self.assertEqual(
            int(match.group(1)),
            _PINNED_WAVE_HEIGHT,
            f"The waveform height is interpolated into the hashed stylesheet. {_REFRESH_HINT}",
        )


if __name__ == "__main__":
    unittest.main()
