#!/usr/bin/env python3
#
# tests/test_runtime_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import db
from app.auth import session
from app.auth.rate_limit import reset_rate_limits
from app.integrations.lalal import client as lalal
from app.main import _governor_config_from_settings
from app.runtime.governor import _auto_concurrent_fragments, governor
from app.utils.template_filters import public_settings
from app.workers import analysis_worker, worker
from tests._support import WebAppTestCase


class RuntimeSettingsTests(WebAppTestCase):
    def test_defaults_are_present_and_public(self) -> None:
        settings = db.get_settings()
        expected = {
            "enable_job_history": True,
            "download_auto_start": True,
            "download_concurrent_fragments": 0,
            "download_worker_count": 0,
            "download_timeout_minutes": 60,
            "transcode_timeout_minutes": 120,
            "download_max_filesize_gib": 4,
            # A freshly initialized database ships the watermark off.
            "video_watermark": False,
            "audio_analysis_max_minutes": 15,
            "audio_analysis_timeout_minutes": 5,
            "lalal_max_download_gib": 4,
        }
        self.assertEqual({key: settings[key] for key in expected}, expected)
        public = public_settings(settings)
        self.assertEqual({key: public[key] for key in expected}, expected)
        settings_page = self.client.get("/settings")
        self.assertEqual(settings_page.status_code, 200)
        self.assertIn('id="enableJobHistory"', settings_page.text)
        self.assertIn('name="enable_job_history" checked', settings_page.text)
        self.assertIn('id="downloadAutoStart"', settings_page.text)
        self.assertIn('name="download_auto_start" checked', settings_page.text)

    def test_download_auto_start_setting_persists_and_reaches_the_dashboard(self) -> None:
        response = self._post_settings({"download_auto_start": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(db.get_settings()["download_auto_start"])
        self.assertFalse(self.client.get("/api/settings").json()["download_auto_start"])

        index_page = self.client.get("/")
        self.assertEqual(index_page.status_code, 200)
        self.assertIn('data-download-auto-start="false"', index_page.text)

        response = self._post_settings({"download_auto_start": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(db.get_settings()["download_auto_start"])

        index_page = self.client.get("/")
        self.assertIn('data-download-auto-start="true"', index_page.text)

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

    def test_job_history_setting_only_affects_future_submissions(self) -> None:
        metadata = {
            "video_title": None,
            "video_meta_hover": None,
            "duration_seconds": None,
        }
        urls = [
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://www.youtube.com/watch?v=lmnopqrstuv",
            "https://www.youtube.com/watch?v=wxyzABCDefg",
        ]
        with (
            patch("app.routes.api.extract_video_meta_async", new=AsyncMock(return_value=metadata)),
            patch("app.routes.api.get_job_queue", return_value=MagicMock(full=lambda: False)),
            patch("app.routes.api.submit_download", return_value=True),
        ):
            headers = {"X-CSRF-Token": self._csrf()}
            first = self.client.post(
                "/api/submit",
                data={"url": urls[0], "type": "audio", "quality": "max"},
                headers=headers,
            )
            self.assertEqual(first.status_code, 200)

            self.assertEqual(self._post_settings({"enable_job_history": False}).status_code, 200)
            hidden = self.client.post(
                "/api/submit",
                data={"url": urls[1], "type": "audio", "quality": "max"},
                headers=headers,
            )
            self.assertEqual(hidden.status_code, 200)

            self.assertEqual(self._post_settings({"enable_job_history": True}).status_code, 200)
            visible = self.client.post(
                "/api/submit",
                data={"url": urls[2], "type": "audio", "quality": "max"},
                headers=headers,
            )
            self.assertEqual(visible.status_code, 200)

        self.assertEqual(db.get_settings()["enable_job_history"], True)
        self.assertEqual(db.get_job(hidden.json()["id"])["include_in_history"], 0)
        self.assertEqual(
            {job["id"] for job in self.client.get("/api/jobs").json()},
            {first.json()["id"], visible.json()["id"]},
        )

    def test_settings_api_rejects_out_of_range_fragments(self) -> None:
        response = self._post_settings({"download_concurrent_fragments": 17})
        self.assertEqual(response.status_code, 400)
        self.assertIn("between 0 and 16", response.json()["detail"])

    def test_settings_api_persists_every_output_mode(self) -> None:
        for mode in db.DOWNLOAD_OUTPUT_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(self._post_settings({"download_output_mode": mode}).status_code, 200)
                self.assertEqual(db.get_settings()["download_output_mode"], mode)
                self.assertEqual(self.client.get("/api/settings").json()["download_output_mode"], mode)

    def test_settings_api_rejects_an_unknown_output_mode(self) -> None:
        response = self._post_settings({"download_output_mode": "vp9"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("download_output_mode must be one of", response.json()["detail"])

    def test_the_output_mode_reaches_the_worker(self) -> None:
        for mode in db.DOWNLOAD_OUTPUT_MODES:
            for watermark in (False, True):
                with self.subTest(mode=mode, watermark=watermark):
                    db.set_settings({"download_output_mode": mode, "video_watermark": watermark})
                    # The watermark forces an encode but never rewrites the mode:
                    # in "source" that encode targets the downloaded codec.
                    self.assertEqual(worker._download_tuning()[1], mode)

    def _post_settings_unthrottled(self, body: dict[str, object]):
        # A sweep over every bound would otherwise trip the settings rate limit.
        reset_rate_limits()
        return self._post_settings(body)

    def test_settings_api_enforces_the_declared_bounds_of_every_integer_setting(self) -> None:
        for key, (minimum, maximum) in db.SETTING_INT_BOUNDS.items():
            if key == "session_max_days":
                continue  # needs a credential round-trip; covered by the session tests
            for value in (minimum, maximum):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self._post_settings_unthrottled({key: value}).status_code, 200)
                    self.assertEqual(db.get_settings()[key], value)
            for value in (minimum - 1, maximum + 1):
                with self.subTest(key=key, rejected=value):
                    response = self._post_settings_unthrottled({key: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["detail"], f"{key} must be between {minimum} and {maximum}")
            with self.subTest(key=key, rejected="text"):
                response = self._post_settings_unthrottled({key: "many"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"], f"{key} must be an integer")

    def test_stored_values_outside_the_declared_bounds_fall_back_to_the_default(self) -> None:
        for key, (minimum, maximum) in db.SETTING_INT_BOUNDS.items():
            with self.subTest(key=key):
                for bad in (minimum - 1, maximum + 1):
                    with db.get_db() as con:
                        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(bad)))
                        con.commit()
                    self.assertEqual(db.get_settings()[key], db.setting_default(key))

    def test_every_integer_bound_contains_its_default(self) -> None:
        for key, (minimum, maximum) in db.SETTING_INT_BOUNDS.items():
            with self.subTest(key=key):
                self.assertTrue(minimum <= db.setting_default(key) <= maximum)

    def test_fallback_defaults_match_the_stored_defaults(self) -> None:
        self.assertEqual(worker._DEFAULT_DOWNLOAD_TIMEOUT_MINUTES, db.setting_default("download_timeout_minutes"))
        self.assertEqual(worker._DEFAULT_TRANSCODE_TIMEOUT_MINUTES, db.setting_default("transcode_timeout_minutes"))
        self.assertEqual(worker._DEFAULT_DOWNLOAD_MAX_FILESIZE_GIB, db.setting_default("download_max_filesize_gib"))
        self.assertEqual(worker._DEFAULT_OUTPUT_MODE, db.setting_default("download_output_mode"))
        self.assertEqual(worker._DEFAULT_VIDEO_WATERMARK, db.setting_default("video_watermark"))
        self.assertEqual(worker._MAX_CONCURRENT_FRAGMENTS, db.SETTING_INT_BOUNDS["download_concurrent_fragments"][1])
        self.assertEqual(lalal._DEFAULT_LALAL_MAX_DOWNLOAD_GIB, db.setting_default("lalal_max_download_gib"))
        self.assertEqual(
            analysis_worker._DEFAULT_MAX_ANALYSIS_MINUTES, db.setting_default("audio_analysis_max_minutes")
        )
        self.assertEqual(
            analysis_worker._DEFAULT_ANALYSIS_TIMEOUT_MINUTES, db.setting_default("audio_analysis_timeout_minutes")
        )
        self.assertEqual(session._DEFAULT_MAX_DAYS, db.setting_default("session_max_days"))

    def test_unreadable_settings_make_the_workers_use_the_stored_defaults(self) -> None:
        with patch.object(worker, "get_settings", side_effect=OSError("db gone")):
            limits = worker._download_runtime_limits()
        self.assertEqual(limits.download_timeout_seconds, db.setting_default("download_timeout_minutes") * 60)
        self.assertEqual(limits.transcode_timeout_seconds, db.setting_default("transcode_timeout_minutes") * 60)
        self.assertEqual(limits.max_filesize_arg, f"{db.setting_default('download_max_filesize_gib')}G")

        with patch.object(analysis_worker, "get_settings", side_effect=OSError("db gone")):
            max_duration_seconds, timeout_seconds = analysis_worker._analysis_runtime_limits()
        self.assertEqual(max_duration_seconds, db.setting_default("audio_analysis_max_minutes") * 60)
        self.assertEqual(timeout_seconds, db.setting_default("audio_analysis_timeout_minutes") * 60)

        with patch.object(lalal, "get_settings", side_effect=OSError("db gone")):
            max_bytes = lalal._max_result_download_bytes()
        self.assertEqual(max_bytes, db.setting_default("lalal_max_download_gib") * 1024**3)

    def test_the_slider_vocabulary_has_not_drifted(self) -> None:
        self.assertEqual(db.DOWNLOAD_OUTPUT_MODES, ("source", "universal", "av1"))

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
