#!/usr/bin/env python3
#
# tests/test_audio_source_header.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""``X-Audio-Quality`` on /audio-source reports provenance, not codec quality."""

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import db
from app.routes import media
from tests._support import WebAppTestCase


class AudioSourceProvenanceHeaderTests(WebAppTestCase):
    def setUp(self) -> None:
        super().setUp()

        from app.main import BASE_DIR, templates

        self.data_dir = Path(self._tmp.name)
        context_patcher = patch.object(
            media,
            "_MEDIA_CONTEXT",
            media.MediaContext(data_dir=self.data_dir, base_dir=BASE_DIR, templates=templates),
        )
        context_patcher.start()
        self.addCleanup(context_patcher.stop)

    def _audio_job(self, filename: str) -> str:
        job_id = str(uuid.uuid4())
        job_dir = self.data_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        file_path = job_dir / filename
        file_path.write_bytes(b"not really audio")
        db.insert_job(job_id, "https://example.com/track", "audio", "max", "queued")
        db.update_job(job_id, status="done", filename=str(file_path))
        return job_id

    def test_a_kept_as_is_source_is_labelled_by_provenance(self) -> None:
        # ".m4a" is a browser-safe container, so no MP3 fallback is involved -
        # and it is lossy, which is exactly why the header must not say
        # "lossless" just because the file was stored unchanged.
        job_id = self._audio_job("track.source.m4a")
        response = self.client.get(f"/audio-source/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Audio-Quality"], "source")

    def test_a_converted_file_is_labelled_converted(self) -> None:
        job_id = self._audio_job("track.mp3")
        response = self.client.get(f"/audio-source/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Audio-Quality"], "converted")


if __name__ == "__main__":
    unittest.main()
