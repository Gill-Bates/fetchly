#!/usr/bin/env python3
#
# tests/test_updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import json
import unittest
from unittest import mock

import httpx

from app.utils import updates

FFMPEG_INSTALLED = "n9.0.2-12-gc867e13549-20260927"
FFMPEG_LATEST = "n9.0.2-36-g6882969ce9"

CHECKSUMS = "\n".join(
    f"{'0' * 64}  {name}"
    for name in (
        "ffmpeg-N-128100-gceb466cbcd-linux64-gpl.tar.xz",
        "ffmpeg-N-128100-gceb466cbcd-linuxarm64-gpl.tar.xz",
        "ffmpeg-n8.1.3-24-g92611900bb-linux64-gpl-8.1.tar.xz",
        "ffmpeg-n8.1.3-24-g92611900bb-linuxarm64-gpl-8.1.tar.xz",
        "ffmpeg-n9.0.2-36-g6882969ce9-linux64-gpl-9.0.tar.xz",
        "ffmpeg-n9.0.2-36-g6882969ce9-linuxarm64-gpl-9.0.tar.xz",
        "ffmpeg-n9.0.2-36-g6882969ce9-win64-gpl-9.0.zip",
    )
)

INSTALLED = {
    "ytdlp": "2026.10.01",
    "ytdlp_ejs": "0.4.0",
    "js_runtime": "2.5.0",
    "ffmpeg": FFMPEG_INSTALLED,
    "wavesurfer": "7.10.0",
}
LATEST = {
    "ytdlp": "2026.10.20",
    "ytdlp_ejs": "0.4.0",
    "js_runtime": "v2.5.0",
    "ffmpeg": FFMPEG_LATEST,
    "wavesurfer": "7.9.0",
}


def _entry(latest: dict[str, str]) -> dict:
    versions = {key: {"version": value, "checked_at": 100.0} for key, value in latest.items()}
    if "ffmpeg" in latest:
        versions["ffmpeg"]["series"] = {
            "9.0": latest["ffmpeg"],
            "8.1": "n8.1.3-24-g92611900bb",
        }
    return {
        "checked_at": 100.0,
        "last_attempt_at": 100.0,
        "complete": True,
        "versions": versions,
    }


class UpdateStatusTests(unittest.IsolatedAsyncioTestCase):
    async def _status(self, installed: dict, latest: dict) -> dict:
        with mock.patch.object(updates, "_get_latest_versions", return_value=_entry(latest)):
            return (await updates.get_update_status(installed))["components"]

    async def test_each_component_reports_current_available_and_unknown(self):
        keys = ("ytdlp", "ytdlp_ejs", "js_runtime", "ffmpeg", "wavesurfer")
        for key in keys:
            with self.subTest(component=key, state="available"):
                newer = {
                    **LATEST,
                    "ytdlp_ejs": "0.5.0",
                    "js_runtime": "v2.6.0",
                    "wavesurfer": "7.11.0",
                }
                components = await self._status(INSTALLED, {key: newer[key]})
                self.assertEqual(components[key]["status"], "available")
                self.assertTrue(components[key]["update_available"])

            with self.subTest(component=key, state="current"):
                current_latest = {
                    "ytdlp": "2026.10.01",
                    "ytdlp_ejs": "0.4.0",
                    "js_runtime": "v2.5.0",
                    "ffmpeg": FFMPEG_INSTALLED.rsplit("-", 1)[0],
                    "wavesurfer": "7.10.0",
                }
                components = await self._status(INSTALLED, {key: current_latest[key]})
                self.assertEqual(components[key]["status"], "current")
                self.assertFalse(components[key]["update_available"])

            with self.subTest(component=key, state="unknown"):
                components = await self._status(INSTALLED, {})
                self.assertEqual(components[key]["status"], "unknown")
                self.assertFalse(components[key]["update_available"])

    async def test_newer_ffmpeg_build_in_same_series_is_an_update(self):
        components = await self._status(INSTALLED, {"ffmpeg": FFMPEG_LATEST})
        self.assertEqual(components["ffmpeg"]["status"], "available")
        self.assertEqual(components["ffmpeg"]["latest"], FFMPEG_LATEST)

    async def test_ffmpeg_compares_within_installed_series(self):
        # Pinned 8.1 image: the newer 9.0 series must not count as an update.
        components = await self._status({"ffmpeg": "n8.1.3-24-g92611900bb-20260927"}, {"ffmpeg": FFMPEG_LATEST})
        self.assertEqual(components["ffmpeg"]["status"], "current")
        self.assertEqual(components["ffmpeg"]["latest"], "n8.1.3-24-g92611900bb")

    async def test_unparseable_installed_version_is_unknown_not_available(self):
        components = await self._status(
            {"ffmpeg": "N-128100-gceb466cbcd", "ytdlp": "unavailable"},
            {"ffmpeg": FFMPEG_LATEST, "ytdlp": "2026.10.20"},
        )
        self.assertEqual(components["ffmpeg"]["status"], "unknown")
        self.assertEqual(components["ytdlp"]["status"], "unknown")


class FfmpegBuildLookupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _client(handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_reads_newest_stable_build_from_newest_autobuild(self):
        tags = [
            {"name": "latest"},
            {"name": "autobuild-2026-10-26-14-25"},
            {"name": "autobuild-2026-10-25-13-00"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tags"):
                return httpx.Response(200, content=json.dumps(tags))
            self.assertIn("autobuild-2026-10-26-14-25", str(request.url))
            return httpx.Response(200, content=CHECKSUMS)

        async with self._client(handler) as client:
            newest, series = await updates._fetch_latest_ffmpeg_build(client)

        self.assertEqual(newest, FFMPEG_LATEST)
        self.assertEqual(series["8.1"], "n8.1.3-24-g92611900bb")

    async def test_failure_yields_unknown_without_affecting_other_components(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "BtbN" in str(request.url) or "FFmpeg-Builds" in str(request.url):
                return httpx.Response(503)
            return httpx.Response(200, content=json.dumps([{"tag_name": "v1.2.3"}]))

        async with self._client(handler) as client:
            results = await updates.asyncio.gather(
                *(updates._fetch_component(client, k, c) for k, c in updates.COMPONENTS.items())
            )

        by_key = {key: tag for key, tag, _retry, _series in results}
        self.assertIsNone(by_key["ffmpeg"])
        self.assertEqual(by_key["ytdlp"], "v1.2.3")
        self.assertEqual(by_key["wavesurfer"], "v1.2.3")


def _release_page(tags: list[str], body_bytes: int = 0) -> str:
    padding = "x" * body_bytes
    return json.dumps([{"tag_name": tag, "draft": False, "prerelease": False, "body": padding} for tag in tags])


class ReleaseLookupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _client(handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=updates._HEADERS)

    async def test_large_release_notes_do_not_make_the_check_fail(self):
        # yt-dlp / deno ship ~50 KB of changelog per release; 20 of them were
        # ~1-1.6 MB and tripped the old 1 MiB response cap on every check.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_release_page(["2026.08.19", "2026.10.21"], 1_500_000))

        async with self._client(handler) as client:
            key, tag, retry, _ = await updates._fetch_component(client, "ytdlp", updates.COMPONENTS["ytdlp"])
        self.assertEqual((key, tag, retry), ("ytdlp", "2026.10.21", False))

    async def test_requests_stay_small_and_carry_user_agent_and_accept(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=_release_page(["v2.9.8"]))

        async with self._client(handler) as client:
            await updates._fetch_latest_release(client, "denoland/deno")
        self.assertEqual(seen[0].url.params["per_page"], str(updates._RELEASES_PER_PAGE))
        self.assertEqual(seen[0].headers["user-agent"], "fetchly-update-check")
        self.assertIn("github", seen[0].headers["accept"])

    async def test_response_over_the_cap_is_still_rejected_as_transient(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_release_page(["1.0.0"], updates._MAX_API_RESPONSE_BYTES))

        async with self._client(handler) as client:
            _, tag, retry, _ = await updates._fetch_component(client, "ytdlp", updates.COMPONENTS["ytdlp"])
        self.assertIsNone(tag)
        self.assertTrue(retry)

    async def test_rate_limit_is_transient_and_not_found_is_not(self):
        for status, expected_retry in ((403, True), (429, True), (404, False)):
            with self.subTest(status=status):
                async with self._client(lambda _request, code=status: httpx.Response(code)) as client:
                    _, tag, retry, _ = await updates._fetch_component(client, "ytdlp", updates.COMPONENTS["ytdlp"])
                self.assertIsNone(tag)
                self.assertEqual(retry, expected_retry)

    async def test_hanging_component_times_out_without_blocking_the_others(self):
        async def slow(_client, _repo):
            await updates.asyncio.sleep(5)

        with (
            mock.patch.object(updates, "_COMPONENT_TIMEOUT", 0.05),
            mock.patch.object(updates, "_fetch_latest_release", slow),
        ):
            async with self._client(lambda _request: httpx.Response(200, content="[]")) as client:
                _, tag, retry, _ = await updates._fetch_component(client, "ytdlp", updates.COMPONENTS["ytdlp"])
        self.assertIsNone(tag)
        self.assertTrue(retry)

    async def test_crash_in_one_component_keeps_the_other_answers(self):
        async def fake(_client, key, _component):
            if key == "ytdlp":
                raise RuntimeError("boom")
            return key, "1.0.0", False, {}

        with (
            mock.patch.object(updates, "_fetch_component", fake),
            self.assertLogs(updates.logger, "ERROR"),
        ):
            versions, complete = await updates._fetch_all({}, 10.0)
        self.assertFalse(complete)
        self.assertNotIn("ytdlp", versions)
        self.assertEqual(versions["wavesurfer"]["version"], "1.0.0")


class VersionComparisonTests(unittest.TestCase):
    def test_calendar_versions_compare_numerically_whatever_the_padding(self):
        self.assertTrue(updates._is_newer("2026.10.21", "2026.08.19"))
        self.assertTrue(updates._is_newer("2026.8.20", "2026.08.19"))
        self.assertFalse(updates._is_newer("2026.08.19", "2026.8.19"))

    def test_v_prefixed_tags_match_plain_installed_versions(self):
        self.assertFalse(updates._is_newer("v2.7.13", "2.7.13"))
        self.assertTrue(updates._is_newer("v2.9.8", "2.7.13"))


class NegativeCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = updates._cache
        updates._cache = None
        patches = (
            mock.patch.object(updates, "_read_cache_file", return_value=None),
            mock.patch.object(updates, "_write_cache_file"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(setattr, updates, "_cache", self._saved)

    async def test_failed_round_is_cached_only_for_the_short_retry_ttl(self):
        async def fake(previous, attempted_at, keys=None):
            return {"ytdlp_ejs": {"version": "0.8.0", "checked_at": attempted_at}}, False

        with mock.patch.object(updates, "_fetch_all", fake):
            entry = await updates._get_latest_versions()
        ttl = entry["expires_at"] - entry["last_attempt_at"]
        self.assertLessEqual(ttl, updates.RETRY_TTL_SECONDS + 5)
        self.assertLess(updates.RETRY_TTL_SECONDS, updates.CACHE_TTL_SECONDS)
        self.assertFalse(entry["complete"])

    async def test_retry_asks_only_the_components_that_failed(self):
        calls: list[frozenset[str] | None] = []

        async def fake(previous, attempted_at, keys=None):
            calls.append(keys)
            good = {k: {"version": "1.0.0", "checked_at": attempted_at} for k in updates.COMPONENTS if k != "ytdlp"}
            return ({**good, "ytdlp": {"version": "2.0.0", "checked_at": attempted_at}} if keys else good), bool(keys)

        with mock.patch.object(updates, "_fetch_all", fake):
            first = await updates._get_latest_versions()
            self.assertFalse(first["complete"])
            updates._cache["expires_at"] = 0.0  # retry window elapsed
            second = await updates._get_latest_versions()

        self.assertIsNone(calls[0])
        self.assertEqual(calls[1], frozenset({"ytdlp"}))
        self.assertTrue(second["complete"])

    async def test_update_status_reports_why_a_check_is_unknown(self):
        entry = _entry({"ytdlp": "2026.10.21", "js_runtime": "v2.9.8"})
        with mock.patch.object(updates, "_get_latest_versions", return_value=entry):
            status = await updates.get_update_status(
                {"ytdlp": "unavailable", "js_runtime": "2.7.13", "wavesurfer": "7.12.6"}
            )
        components = status["components"]
        self.assertEqual(components["ytdlp"]["reason"], "installed_unknown")
        self.assertEqual(components["wavesurfer"]["reason"], "latest_unknown")
        self.assertEqual(components["js_runtime"]["status"], "available")
        self.assertEqual(components["js_runtime"]["reason"], "")


if __name__ == "__main__":
    unittest.main()
