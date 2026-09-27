#!/usr/bin/env python3
#
# tests/test_client_ip_geoip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The job detail page's "requester" tile: client_ip persistence, the
geoip module's public/private guard, graceful degradation when no
GeoLite2 database is present (the default in this test environment), the
v2->v3 schema migration that added the column, and the download/staleness
logic in isolation from the real network."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import db
from app.routes import media
from app.utils import geoip
from app.utils.geoip import build_requester_info
from tests._support import IsolatedDbTestCase, WebAppTestCase


class InsertJobClientIpTests(IsolatedDbTestCase):
    def test_insert_job_persists_ipv4_client_ip(self) -> None:
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/video", "video", "max", "queued", client_ip="203.0.113.10")

        job = db.get_job(job_id)

        assert job is not None
        self.assertEqual(job["client_ip"], "203.0.113.10")

    def test_insert_job_persists_ipv6_client_ip(self) -> None:
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/video", "video", "max", "queued", client_ip="2001:db8::1")

        job = db.get_job(job_id)

        assert job is not None
        self.assertEqual(job["client_ip"], "2001:db8::1")

    def test_insert_job_leaves_client_ip_null_by_default(self) -> None:
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/video", "video", "max", "queued")

        job = db.get_job(job_id)

        assert job is not None
        self.assertIsNone(job["client_ip"])


class SchemaMigrationTests(IsolatedDbTestCase):
    def test_a_v2_database_gains_the_client_ip_column_on_open(self) -> None:
        # Recreates the exact v2 shape (no client_ip, user_version = 2) that
        # a deployment upgrading from before this feature would have on
        # disk, then confirms init_db() adds the column and bumps the stamp
        # without losing the existing row.
        # setUp() already ran init_db() at the current schema; drop that
        # table and reset the stamp to recreate the exact v2 on-disk shape.
        db.close_db()
        con = sqlite3.connect(db.DB_PATH)
        try:
            con.execute("DROP TABLE IF EXISTS jobs")
            con.execute("PRAGMA user_version = 2")
            con.execute("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    type TEXT,
                    quality TEXT,
                    status TEXT NOT NULL,
                    filename TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    include_in_history INTEGER NOT NULL DEFAULT 1,
                    finished_at TEXT,
                    duration_seconds REAL,
                    filesize_bytes INTEGER,
                    message TEXT,
                    codec TEXT,
                    bitrate_kbps INTEGER,
                    video_title TEXT,
                    video_meta_hover TEXT,
                    bpm INTEGER,
                    bpm_confidence REAL,
                    audio_hash TEXT,
                    lalal_split_done INTEGER NOT NULL DEFAULT 0
                )
            """)
            con.execute(
                "INSERT INTO jobs (id, url, type, quality, status) VALUES (?, ?, ?, ?, ?)",
                ("pre-existing", "https://example.com/video", "video", "max", "queued"),
            )
            con.commit()
        finally:
            con.close()

        db._database_path_prepared = None
        db.init_db()

        job = db.get_job("pre-existing")
        assert job is not None
        self.assertIsNone(job["client_ip"])
        with db.get_db() as con:
            self.assertEqual(int(con.execute("PRAGMA user_version").fetchone()[0]), 3)


class PublicIpGuardTests(IsolatedDbTestCase):
    def test_rejects_private_and_loopback_addresses(self) -> None:
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "::1", "fc00::1", "not-an-ip"):
            with self.subTest(ip=ip):
                self.assertFalse(geoip._public_ip(ip))

    def test_accepts_global_addresses_of_both_families(self) -> None:
        # 8.8.8.8 / 2001:4860:4860::8888 (Google public DNS) are real,
        # globally routable addresses - unlike the 203.0.113.0/24 "TEST-NET-3"
        # documentation range used elsewhere in this file, which
        # ipaddress.is_global correctly reports as *not* global.
        for ip in ("8.8.8.8", "2001:4860:4860::8888"):
            with self.subTest(ip=ip):
                self.assertTrue(geoip._public_ip(ip))

    def test_lookup_ip_returns_none_without_downloaded_databases(self) -> None:
        # Explicitly simulate "no .mmdb files present" rather than relying on
        # the host's ambient DATA_DIR being empty - a real GeoLite2 download
        # on the machine running this suite must not make the test flaky.
        with patch.object(geoip._city_reader, "get", return_value=None), patch.object(
            geoip._asn_reader, "get", return_value=None
        ):
            self.assertIsNone(geoip.lookup_ip("8.8.8.8"))

    def test_lookup_ip_returns_none_for_private_address_even_with_a_reader(self) -> None:
        with patch.object(geoip, "_city_reader") as city_reader, patch.object(geoip, "_asn_reader") as asn_reader:
            self.assertIsNone(geoip.lookup_ip("10.0.0.5"))
            city_reader.get.assert_not_called()
            asn_reader.get.assert_not_called()


class DownloadUrlAllowlistTests(IsolatedDbTestCase):
    def test_the_configured_source_urls_are_allowed(self) -> None:
        geoip._validate_download_url(geoip._CITY_SPEC.url)
        geoip._validate_download_url(geoip._ASN_SPEC.url)

    def test_the_github_raw_redirect_target_is_allowed(self) -> None:
        # github.com's /raw/ path always 302s here before serving the file;
        # rejecting this host is exactly what made every download fail.
        geoip._validate_download_url("https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/x.mmdb")

    def test_an_unrelated_host_is_rejected_even_over_https(self) -> None:
        with self.assertRaises(ValueError):
            geoip._validate_download_url("https://evil.example.com/GeoLite2-City.mmdb")

    def test_a_non_https_scheme_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            geoip._validate_download_url("http://github.com/P3TERX/GeoLite.mmdb/raw/download/x.mmdb")


class StalenessRefreshTests(IsolatedDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        # geoip._geoip_dir() reads DATA_DIR through app.utils.fs.get_data_dir();
        # redirect it into the per-test tempdir so these tests never touch
        # the real ./data/geolite2 directory.
        patcher = patch.object(geoip, "get_data_dir", lambda: Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_fake_mmdb(self, spec: geoip._DBSpec, *, age_seconds: float) -> Path:
        path = geoip._db_path(spec)
        path.write_bytes(b"0" * spec.min_size)
        old = time.time() - age_seconds
        import os

        os.utime(path, (old, old))
        return path

    def test_a_fresh_valid_file_is_left_alone(self) -> None:
        with patch.object(geoip, "_verify_mmdb", return_value=True):
            self._write_fake_mmdb(geoip._CITY_SPEC, age_seconds=60)
            with patch("app.utils.geoip.httpx.Client") as client_cls:
                downloaded = geoip._download_one(geoip._CITY_SPEC)
            client_cls.assert_not_called()
        self.assertFalse(downloaded)

    def test_a_file_older_than_the_refresh_window_is_re_downloaded(self) -> None:
        with patch.object(geoip, "_verify_mmdb", return_value=True):
            self._write_fake_mmdb(geoip._CITY_SPEC, age_seconds=geoip._STALE_AFTER_SECONDS + 1)
            with patch.object(geoip, "_validate_download_url"), patch("app.utils.geoip.httpx.Client") as client_cls:
                client_cls.return_value.__enter__.return_value.stream.side_effect = RuntimeError("network disabled")
                downloaded = geoip._download_one(geoip._CITY_SPEC)
            client_cls.assert_called_once()
        # The refresh attempt was made (proving staleness triggered it); it
        # failed here by design (network mocked out), so nothing was written.
        self.assertFalse(downloaded)

    def test_is_stale_reports_true_for_a_missing_file(self) -> None:
        self.assertTrue(geoip._is_stale(geoip._db_path(geoip._CITY_SPEC)))


class BuildRequesterInfoTests(IsolatedDbTestCase):
    def test_returns_none_without_a_client_ip(self) -> None:
        self.assertIsNone(build_requester_info(None))
        self.assertIsNone(build_requester_info(""))

    def test_shows_the_ip_alone_when_geoip_has_no_data(self) -> None:
        with patch.object(geoip, "lookup_ip", return_value=None):
            info = build_requester_info("203.0.113.10")

        self.assertEqual(info, {"ip": "203.0.113.10"})

    def test_ipv6_is_rendered_in_compressed_form(self) -> None:
        with patch.object(geoip, "lookup_ip", return_value=None):
            info = build_requester_info("2001:0db8:0000:0000:0000:0000:0000:0001")

        assert info is not None
        self.assertEqual(info["ip"], "2001:db8::1")

    def test_geo_fields_are_included_when_present_and_omitted_when_missing(self) -> None:
        with patch.object(
            geoip,
            "lookup_ip",
            return_value={"country": "DE", "city": "Berlin", "asn": 3320, "as_org": "Deutsche Telekom"},
        ):
            info = build_requester_info("203.0.113.10")

        self.assertEqual(
            info,
            {
                "ip": "203.0.113.10",
                "country": "DE",
                "city": "Berlin",
                "asn": 3320,
                "as_org": "Deutsche Telekom",
            },
        )

        empty_geo = {"country": None, "city": None, "asn": None, "as_org": None}
        with patch.object(geoip, "lookup_ip", return_value=empty_geo):
            info = build_requester_info("203.0.113.10")

        self.assertEqual(info, {"ip": "203.0.113.10"})

    def test_rejects_an_unparseable_client_ip(self) -> None:
        self.assertIsNone(build_requester_info("not-an-ip"))


class JobPageRequesterTileTests(WebAppTestCase):
    def setUp(self) -> None:
        super().setUp()
        # WebAppTestCase only runs init_api(); the job page also depends on
        # media.py's own module-level context (data_dir/base_dir/templates).
        from app.main import BASE_DIR, DATA_DIR, templates

        media.init_media(DATA_DIR, BASE_DIR, templates)

    def _job(self, **kwargs: object) -> str:
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/video", "video", "max", "queued", **kwargs)
        return job_id

    def test_tile_is_omitted_when_no_client_ip_was_recorded(self) -> None:
        job_id = self._job()

        response = self.client.get(f"/job/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Requester", response.text)

    def test_tile_shows_the_ip_when_geoip_has_no_data(self) -> None:
        job_id = self._job(client_ip="203.0.113.10")

        with patch("app.utils.geoip.lookup_ip", return_value=None):
            response = self.client.get(f"/job/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Requester", response.text)
        self.assertIn("203.0.113.10", response.text)

    def test_tile_shows_ipv6_country_and_asn_when_geoip_resolves(self) -> None:
        job_id = self._job(client_ip="2001:db8::1")

        with patch(
            "app.utils.geoip.lookup_ip",
            return_value={"country": "DE", "city": "Berlin", "asn": 3320, "as_org": "Deutsche Telekom"},
        ):
            response = self.client.get(f"/job/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Requester", response.text)
        self.assertIn("2001:db8::1", response.text)
        self.assertIn("https://cdn.jsdelivr.net/npm/flag-icons@7.3.2/flags/4x3/de.svg", response.text)
        self.assertIn("Berlin", response.text)
        self.assertIn("AS3320", response.text)

    def test_share_error_page_never_renders_a_requester_tile(self) -> None:
        # The public share view renders a dedicated template that carries no
        # job/requester context at all - confirm that contract directly
        # rather than just asserting an absence of the word "Requester".
        response = self.client.get("/share/does-not-exist")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Requester", response.text)
        self.assertNotIn("client_ip", response.text)

    def test_csp_allows_jsdelivr_for_images_only(self) -> None:
        # The flag SVGs are loaded live from cdn.jsdelivr.net (never
        # vendored, see app/templates/job.html) - the CSP exception for that
        # must stay scoped to img-src and not leak into any other directive.
        from app.main import SecurityHeadersMiddleware

        directives = SecurityHeadersMiddleware._CSP.split("; ")
        img_src = next(part for part in directives if part.startswith("img-src"))
        self.assertIn("https://cdn.jsdelivr.net", img_src)

        for part in directives:
            if part.startswith("img-src"):
                continue
            self.assertNotIn("jsdelivr.net", part)


class MmdbVerificationTests(IsolatedDbTestCase):
    """S9: the mirror publishes no checksum file, so a corrupted body must be
    caught by a real data read, not just a metadata parse."""

    def _write_min_size_file(self, spec: geoip._DBSpec) -> Path:
        path = Path(self._tmp.name) / spec.filename
        path.write_bytes(b"0" * spec.min_size)
        return path

    def _fake_reader(self, *, database_type: str, lookup_error: Exception | None) -> MagicMock:
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.__exit__.return_value = False
        reader.metadata.return_value = SimpleNamespace(database_type=database_type)
        if lookup_error is not None:
            reader.city.side_effect = lookup_error
            reader.asn.side_effect = lookup_error
        return reader

    def test_a_corrupted_body_fails_verification_even_with_valid_metadata(self) -> None:
        path = self._write_min_size_file(geoip._CITY_SPEC)
        fake_reader = self._fake_reader(database_type="GeoLite2-City", lookup_error=RuntimeError("corrupt body"))

        with patch.object(geoip.geoip2.database, "Reader", return_value=fake_reader):
            self.assertFalse(geoip._verify_mmdb(path, geoip._CITY_SPEC))

    def test_a_smoke_test_miss_is_not_treated_as_corruption(self) -> None:
        path = self._write_min_size_file(geoip._CITY_SPEC)
        fake_reader = self._fake_reader(
            database_type="GeoLite2-City", lookup_error=geoip.geoip2.errors.AddressNotFoundError("not found")
        )

        with patch.object(geoip.geoip2.database, "Reader", return_value=fake_reader):
            self.assertTrue(geoip._verify_mmdb(path, geoip._CITY_SPEC))

    def test_sha256_file_matches_a_known_digest(self) -> None:
        path = Path(self._tmp.name) / "sample.bin"
        path.write_bytes(b"fetchly-geoip-test")

        import hashlib

        expected = hashlib.sha256(b"fetchly-geoip-test").hexdigest()
        self.assertEqual(geoip._sha256_file(path), expected)


class ReaderCloseRaceTests(IsolatedDbTestCase):
    """N4: close_readers() must not tear down a reader an in-flight lookup holds."""

    def test_close_blocks_until_an_in_flight_lookup_releases_the_reader(self) -> None:
        manager = geoip._ReaderManager(geoip._CITY_SPEC)
        fake_reader = MagicMock()
        manager._reader = fake_reader

        order: list[str] = []
        lookup_entered = threading.Event()
        allow_lookup_to_finish = threading.Event()

        def lookup() -> None:
            with manager.use() as reader:
                order.append("lookup_start")
                self.assertIs(reader, fake_reader)
                lookup_entered.set()
                allow_lookup_to_finish.wait(timeout=5)
                order.append("lookup_end")

        def closer() -> None:
            lookup_entered.wait(timeout=5)
            manager.close()
            order.append("close_done")

        lookup_thread = threading.Thread(target=lookup)
        close_thread = threading.Thread(target=closer)
        lookup_thread.start()
        close_thread.start()

        self.assertTrue(lookup_entered.wait(timeout=5))
        # close() is holding on the same lock the lookup entered with; it
        # must not have completed while the lookup is still inside `use()`.
        close_thread.join(timeout=0.2)
        self.assertTrue(close_thread.is_alive())
        self.assertNotIn("close_done", order)

        allow_lookup_to_finish.set()
        lookup_thread.join(timeout=5)
        close_thread.join(timeout=5)

        self.assertFalse(lookup_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(order, ["lookup_start", "lookup_end", "close_done"])
        fake_reader.close.assert_called_once()
        self.assertIsNone(manager._reader)

    def test_use_yields_none_when_no_reader_is_open(self) -> None:
        manager = geoip._ReaderManager(geoip._CITY_SPEC)
        with patch.object(manager, "get", return_value=None) as get_mock:
            with manager.use() as reader:
                self.assertIsNone(reader)
            get_mock.assert_called_once()

    def test_a_lookup_after_a_completed_close_sees_none(self) -> None:
        with patch.object(geoip, "_city_reader") as city_reader, patch.object(geoip, "_asn_reader") as asn_reader:
            city_reader.use.return_value.__enter__.return_value = None
            asn_reader.use.return_value.__enter__.return_value = None
            geoip._cached_lookup.cache_clear()
            self.assertIsNone(geoip._cached_lookup("8.8.8.8", 0))
