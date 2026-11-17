#!/usr/bin/env python3
#
# tests/test_lalal_client_io.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""LalalClient transfers: no blocking I/O on the event loop, errors surface as LalalError."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from unittest.mock import patch

import httpx

from app.integrations.lalal import client as lalal_client
from app.integrations.lalal.client import LalalClient, LalalError, LalalUnavailableError, LalalUploadError

_URL = "https://cdn.lalal.ai/stem.mp3?token=secret-token"


def _real_async_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    real = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    return factory


class DownloadResultTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.client = LalalClient("test-key")

    def _download(self, handler: Callable[[httpx.Request], httpx.Response], *, limit: int = 1024) -> Path:
        out = self.dir / "sub" / "stem.mp3"
        with (
            patch.object(lalal_client.httpx, "AsyncClient", _real_async_client(handler)),
            patch.object(lalal_client, "_max_result_download_bytes", return_value=limit),
        ):
            return asyncio.run(self.client.download_result(_URL, out))

    def test_successful_download_writes_the_file(self) -> None:
        result = self._download(lambda _req: httpx.Response(200, content=b"abc" * 10))
        self.assertEqual(result.read_bytes(), b"abc" * 10)
        self.assertEqual(list(result.parent.glob("*.part")), [])

    def test_http_error_becomes_lalal_error_without_leaking_the_url(self) -> None:
        with self.assertRaises(LalalError) as ctx:
            self._download(lambda _req: httpx.Response(403))
        self.assertIn("403", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))
        self.assertNotIn("cdn.lalal.ai", str(ctx.exception))
        self.assertEqual(list((self.dir / "sub").glob("*")), [])

    def test_transport_error_becomes_lalal_error(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom secret-token")

        with self.assertRaises(LalalError) as ctx:
            self._download(handler)
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_partial_file_is_removed_when_the_size_limit_aborts(self) -> None:
        # No content-length announced up front, so the limit trips mid-stream.
        async def body() -> AsyncIterator[bytes]:
            yield b"x" * 600
            yield b"x" * 600

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body())

        with self.assertRaises(LalalError):
            self._download(handler, limit=1000)
        self.assertEqual(list((self.dir / "sub").glob("*")), [])

    def test_download_limit_is_read_off_the_event_loop_thread(self) -> None:
        seen: dict[str, int] = {}

        async def run() -> None:
            seen["loop"] = threading.get_ident()
            out = self.dir / "stem.mp3"
            await self.client.download_result(_URL, out)

        def fake_limit() -> int:
            seen["read"] = threading.get_ident()
            return 1024

        with (
            patch.object(
                lalal_client.httpx,
                "AsyncClient",
                _real_async_client(lambda _r: httpx.Response(200, content=b"a")),
            ),
            patch.object(lalal_client, "_max_result_download_bytes", fake_limit),
        ):
            asyncio.run(run())
        self.assertNotEqual(seen["read"], seen["loop"])


class UploadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / "song.mp3"
        self.source.write_bytes(b"audio-bytes")

    def _upload(self, response: httpx.Response) -> dict[str, object]:
        async def run() -> dict[str, object]:
            client = LalalClient("test-key")
            client._client = httpx.AsyncClient(
                base_url=lalal_client.LALAL_API_BASE,
                transport=httpx.MockTransport(lambda _req: response),
            )
            try:
                return dict(await client.upload_file(self.source))
            finally:
                await client.close()

        return asyncio.run(run())

    def test_upload_returns_the_file_id(self) -> None:
        result = self._upload(httpx.Response(200, json={"id": "abc123", "duration": 12.5}))
        self.assertEqual(result["id"], "abc123")

    def test_upload_without_an_id_is_rejected_immediately(self) -> None:
        for body in ({}, {"id": ""}, {"id": None}, {"name": "song.mp3"}):
            with self.subTest(body=body), self.assertRaises(LalalUploadError):
                self._upload(httpx.Response(200, json=body))

    def test_missing_source_file_still_raises_file_not_found(self) -> None:
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            self._upload(httpx.Response(200, json={"id": "x"}))


class CheckQuotaStatusTests(unittest.TestCase):
    def _check(self, handler: Callable[[httpx.Request], httpx.Response]) -> dict[str, object]:
        async def run() -> dict[str, object]:
            client = LalalClient("test-key")
            client._client = httpx.AsyncClient(
                base_url=lalal_client.LALAL_API_BASE,
                transport=httpx.MockTransport(handler),
            )
            try:
                return await client.check_quota()
            finally:
                await client.close()

        return asyncio.run(run())

    def test_only_rejected_keys_raise_a_plain_lalal_error(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(LalalError) as ctx:
                self._check(lambda _req, status=status: httpx.Response(status, json={"detail": "nope"}))
            self.assertNotIsInstance(ctx.exception, LalalUnavailableError)

    def test_transient_statuses_do_not_look_like_a_rejected_key(self) -> None:
        for status in (402, 429, 500, 503):
            with self.subTest(status=status), self.assertRaises(LalalUnavailableError):
                self._check(lambda _req, status=status: httpx.Response(status, text="<html>busy</html>"))

    def test_timeout_propagates_as_a_transport_error(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        with self.assertRaises(httpx.TimeoutException):
            self._check(handler)

    def test_success_returns_the_payload(self) -> None:
        self.assertEqual(self._check(lambda _req: httpx.Response(200, json={"minutes_left": 5})), {"minutes_left": 5})


class ProcessFileCleanupTests(unittest.TestCase):
    def _run(self, out: Path, *, backing_fails: bool) -> dict[str, Path]:
        source = out / "upload.wav"
        source.write_bytes(b"wav")
        client = LalalClient("test-key")

        async def download(_url: str, path: Path, **_kwargs: object) -> Path:
            if backing_fails and path.name.endswith("_instrumental.wav"):
                raise LalalError("backing download failed")
            Path(path).write_bytes(b"stem")
            return Path(path)

        async def run() -> dict[str, Path]:
            with (
                patch.object(client, "upload_file", return_value={"id": "f1"}),
                patch.object(client, "split", return_value="t1"),
                patch.object(
                    client,
                    "wait_for_completion",
                    return_value={"stem_track": "https://x/s", "back_track": "https://x/b"},
                ),
                patch.object(client, "download_result", side_effect=download),
            ):
                return await client.process_file(source, out)

        return asyncio.run(run())

    def test_failed_backing_download_removes_the_vocal_intermediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            keep = out / "unrelated_vocals.mp3"
            keep.write_bytes(b"cached")
            with self.assertRaises(LalalError):
                self._run(out, backing_fails=True)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["unrelated_vocals.mp3", "upload.wav"])

    def test_successful_run_keeps_both_stems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = self._run(Path(tmp), backing_fails=False)
            self.assertTrue(results["stem"].is_file())
            self.assertTrue(results["backing"].is_file())


if __name__ == "__main__":
    unittest.main()
