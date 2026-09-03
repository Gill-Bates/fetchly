#!/usr/bin/env python3
#
# tests/test_runtime_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-runtime-settings-secret")

from fastapi.testclient import TestClient

from app import analysis_worker, db, lalal, worker
from app.governor import _auto_concurrent_fragments, governor
from app.main import _governor_config_from_settings, app, templates
from app.routes.api import init_api
from app.session import refresh_session_settings_cache
from app.utils.template_filters import public_settings


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        patcher = patch.object(db, "DB_PATH", Path(self._tmp.name) / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db._database_path_prepared = None
        self.addCleanup(setattr, db, "_database_path_prepared", None)
        self.addCleanup(db.close_db)
        db.init_db()

        init_api(templates)
        refresh_session_settings_cache()

        from app.common.rate_limit import limiter

        limiter.reset()
        self.addCleanup(limiter.reset)

        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def _csrf(self) -> str:
        self.client.get("/login")
        token = self.client.cookies.get("fetchly_csrf")
        self.assertTrue(token)
        return str(token)

    def _post_settings(self, body: dict[str, object]):
        return self.client.post(
            "/api/settings",
            json=body,
            headers={"X-CSRF-Token": self._csrf()},
        )

    def test_defaults_are_present_and_public(self) -> None:
        settings = db.get_settings()
        expected = {
            "download_concurrent_fragments": 0,
            "download_worker_count": 0,
            "download_timeout_minutes": 60,
            "transcode_timeout_minutes": 120,
            "download_max_filesize_gib": 4,
            "audio_analysis_max_minutes": 15,
            "audio_analysis_timeout_minutes": 5,
            "lalal_max_download_gib": 4,
        }
        self.assertEqual({key: settings[key] for key in expected}, expected)
        public = public_settings(settings)
        self.assertEqual({key: public[key] for key in expected}, expected)

    def test_settings_api_persists_runtime_limits(self) -> None:
        response = self._post_settings(
            {
                "download_worker_count": 6,
                "download_timeout_minutes": 75,
                "transcode_timeout_minutes": 180,
                "download_max_filesize_gib": 12,
                "audio_analysis_max_minutes": 0,
                "audio_analysis_timeout_minutes": 9,
                "lalal_max_download_gib": 7,
            }
        )
        self.assertEqual(response.status_code, 200)

        settings = db.get_settings()
        self.assertEqual(settings["download_worker_count"], 6)
        self.assertEqual(settings["download_timeout_minutes"], 75)
        self.assertEqual(settings["transcode_timeout_minutes"], 180)
        self.assertEqual(settings["download_max_filesize_gib"], 12)
        self.assertEqual(settings["audio_analysis_max_minutes"], 0)
        self.assertEqual(settings["audio_analysis_timeout_minutes"], 9)
        self.assertEqual(settings["lalal_max_download_gib"], 7)

        payload = self.client.get("/api/settings").json()
        self.assertEqual(payload["download_worker_count"], 6)
        self.assertEqual(payload["audio_analysis_max_minutes"], 0)

    def test_settings_api_rejects_out_of_range_runtime_limit(self) -> None:
        response = self._post_settings({"download_worker_count": 9})
        self.assertEqual(response.status_code, 400)
        self.assertIn("between 0 and 8", response.json()["detail"])

    def test_worker_runtime_limits_follow_settings(self) -> None:
        db.set_settings(
            {
                "download_timeout_minutes": 2,
                "transcode_timeout_minutes": 3,
                "download_max_filesize_gib": 5,
            }
        )
        limits = worker._download_runtime_limits()
        self.assertEqual(limits.download_timeout_seconds, 120)
        self.assertEqual(limits.transcode_timeout_seconds, 180)
        self.assertEqual(limits.max_filesize_arg, "5G")

    def test_analysis_runtime_limits_treat_zero_as_unlimited(self) -> None:
        db.set_settings(
            {
                "audio_analysis_max_minutes": 0,
                "audio_analysis_timeout_minutes": 7,
            }
        )
        max_duration_seconds, timeout_seconds = analysis_worker._analysis_runtime_limits()
        self.assertIsNone(max_duration_seconds)
        self.assertEqual(timeout_seconds, 420)

    def test_lalal_limit_uses_persisted_gib_setting(self) -> None:
        db.set_settings({"lalal_max_download_gib": 9})
        self.assertEqual(lalal._max_result_download_bytes(), 9 * 1024 * 1024 * 1024)

    def test_governor_config_uses_persisted_worker_count(self) -> None:
        config = _governor_config_from_settings({"download_worker_count": 4})
        self.assertEqual(config.worker_count, 4)

    def test_settings_api_accepts_automatic_fragments(self) -> None:
        self.assertEqual(self._post_settings({"download_concurrent_fragments": 6}).status_code, 200)
        self.assertEqual(db.get_settings()["download_concurrent_fragments"], 6)

        self.assertEqual(self._post_settings({"download_concurrent_fragments": 0}).status_code, 200)
        self.assertEqual(db.get_settings()["download_concurrent_fragments"], 0)

    def test_settings_api_persists_the_video_watermark_switch(self) -> None:
        response = self._post_settings({"video_watermark": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(db.get_settings()["video_watermark"])
        self.assertFalse(self.client.get("/api/settings").json()["video_watermark"])
        self.assertFalse(worker._watermark_config()[0])

        response = self._post_settings({"video_watermark": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(db.get_settings()["video_watermark"])
        self.assertTrue(worker._watermark_config()[0])

    def test_settings_api_rejects_out_of_range_fragments(self) -> None:
        response = self._post_settings({"download_concurrent_fragments": 17})
        self.assertEqual(response.status_code, 400)
        self.assertIn("between 0 and 16", response.json()["detail"])

    def test_manual_fragment_count_skips_host_probe(self) -> None:
        db.set_settings({"download_concurrent_fragments": 9})
        with patch.object(governor, "recommended_concurrent_fragments") as probe:
            fragments, _ = worker._download_tuning()
        self.assertEqual(fragments, 9)
        probe.assert_not_called()

    def test_automatic_fragment_count_comes_from_the_host(self) -> None:
        db.set_settings({"download_concurrent_fragments": 0})
        with patch.object(governor, "recommended_concurrent_fragments", return_value=5):
            fragments, _ = worker._download_tuning()
        self.assertEqual(fragments, 5)

    def test_automatic_fragment_count_falls_back_when_probe_fails(self) -> None:
        db.set_settings({"download_concurrent_fragments": 0})
        with patch.object(governor, "recommended_concurrent_fragments", side_effect=OSError("no cgroup")):
            fragments, _ = worker._download_tuning()
        self.assertEqual(fragments, worker._DEFAULT_CONCURRENT_FRAGMENTS)


class AutoFragmentSizingTests(unittest.TestCase):
    """Host-capacity sizing behind the "Automatic" fragment setting."""

    def test_scales_with_cpu_quota_within_bounds(self) -> None:
        # -1 memory means "unknown", so only the CPU quota is taken into account.
        self.assertEqual(_auto_concurrent_fragments(0.5, -1), 2)
        self.assertEqual(_auto_concurrent_fragments(1.0, -1), 2)
        self.assertEqual(_auto_concurrent_fragments(2.0, -1), 3)
        self.assertEqual(_auto_concurrent_fragments(4.0, -1), 6)
        self.assertEqual(_auto_concurrent_fragments(16.0, -1), 8)

    def test_low_free_memory_lowers_the_count(self) -> None:
        self.assertEqual(_auto_concurrent_fragments(8.0, 4096), 8)
        self.assertEqual(_auto_concurrent_fragments(8.0, 256), 4)
        # Never below the floor, even on a host with nothing left.
        self.assertEqual(_auto_concurrent_fragments(8.0, 0), 2)

    def test_memory_never_raises_the_cpu_derived_count(self) -> None:
        self.assertEqual(_auto_concurrent_fragments(1.0, 65536), 2)


if __name__ == "__main__":
    unittest.main()
