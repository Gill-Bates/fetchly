#!/usr/bin/env python3
#
# tests/test_csp_wavesurfer.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Guards the CSP mechanism that lets WaveSurfer's shadow-DOM stylesheet load.

Under a bare `style-src 'self'` the browser blocks WaveSurfer's shadow-root
<style>, and the progress canvas renders as a second waveform. WaveSurfer is
now fetched unpinned at build time, so its stylesheet text (which interpolates
the waveform height) is not fixed and a hard-coded style hash would drift.
Instead the app emits a per-request nonce in `style-src` and trim.js stamps the
same nonce onto WaveSurfer's injected <style> via `WaveSurfer.create({ cspNonce })`.

These tests pin that contract: the policy carries a scoped nonce rather than
'unsafe-inline', the nonce is fresh per response, the rendered page exposes the
same nonce the header carries, and trim.js actually feeds it to WaveSurfer.
"""

import re
import unittest
from pathlib import Path

from app.main import build_csp
from tests._support import WebAppTestCase

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TRIM_JS = _REPO_ROOT / "app/static/js/trim.js"

_STYLE_SRC_RE = re.compile(r"style-src ([^;]*)")
_HEADER_NONCE_RE = re.compile(r"style-src [^;]*'nonce-([^']+)'")
_DATASET_NONCE_RE = re.compile(r'data-wavesurfer-csp-nonce="([^"]*)"')


class WaveSurferCspNonceTests(unittest.TestCase):
    def test_policy_uses_a_scoped_nonce_without_unsafe_inline(self) -> None:
        csp = build_csp("test-nonce-value")
        directive = next(part for part in csp.split("; ") if part.startswith("style-src"))
        self.assertIn("'self'", directive)
        self.assertIn("'nonce-test-nonce-value'", directive)
        # The nonce exists precisely so the blanket keyword is not needed.
        self.assertNotIn("unsafe-inline", directive)

    def test_trim_js_passes_the_nonce_to_wavesurfer(self) -> None:
        source = _TRIM_JS.read_text(encoding="utf-8")
        self.assertIn("cspNonce", source, "trim.js no longer forwards the CSP nonce to WaveSurfer")
        self.assertIn(
            "wavesurferCspNonce",
            source,
            "trim.js no longer reads the nonce from the page dataset",
        )


class WaveSurferCspNonceRenderTests(WebAppTestCase):
    def test_rendered_page_nonce_matches_the_response_header(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        csp = response.headers.get("Content-Security-Policy", "")
        header_match = _HEADER_NONCE_RE.search(csp)
        self.assertIsNotNone(header_match, f"no style-src nonce in CSP header: {csp!r}")
        header_nonce = header_match.group(1)
        self.assertNotIn("unsafe-inline", _STYLE_SRC_RE.search(csp).group(1))

        dataset_match = _DATASET_NONCE_RE.search(response.text)
        self.assertIsNotNone(dataset_match, "dashboard did not expose data-wavesurfer-csp-nonce")
        self.assertEqual(
            dataset_match.group(1),
            header_nonce,
            "the page's WaveSurfer nonce must equal the one in the CSP header",
        )

    def test_nonce_is_fresh_per_response(self) -> None:
        first = self.client.get("/").headers.get("Content-Security-Policy", "")
        second = self.client.get("/").headers.get("Content-Security-Policy", "")
        first_nonce = _HEADER_NONCE_RE.search(first)
        second_nonce = _HEADER_NONCE_RE.search(second)
        self.assertIsNotNone(first_nonce)
        self.assertIsNotNone(second_nonce)
        self.assertNotEqual(
            first_nonce.group(1),
            second_nonce.group(1),
            "a per-request nonce must not repeat across responses",
        )


if __name__ == "__main__":
    unittest.main()
