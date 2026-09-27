#!/usr/bin/env python3
#
# app/utils/geoip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""MaxMind GeoLite2 lookups for the job detail page's "requester" tile.

Resolves a stored ``jobs.client_ip`` into country/city/ASN at render time
(see app/routes/media.py::job_page) rather than persisting the resolved
fields, so the answer always reflects the current databases. Lookups are
cheap and cached (functools.lru_cache) once the databases are present.

The City and ASN databases are downloaded on demand from the P3TERX
GeoLite.mmdb mirror (no MaxMind license key required) and cached under
``DATA_DIR/geolite2``. Until a first successful download, lookup_ip()
degrades to None - the caller renders the requester tile without geo data.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Required, TypedDict
from urllib.parse import urlparse

import httpx

from .fs import get_data_dir

if TYPE_CHECKING:
    import geoip2.database

logger = logging.getLogger(__name__)

__all__ = [
    "IPInfo",
    "RequesterInfo",
    "build_requester_info",
    "close_readers",
    "ensure_geoip_databases",
    "ensure_geoip_databases_async",
    "lookup_ip",
]

try:
    import geoip2.database
    import geoip2.errors

    _HAS_GEOIP2 = True
except ImportError:  # pragma: no cover - geoip2 is a required dependency
    _HAS_GEOIP2 = False
    geoip2 = None  # type: ignore[assignment]

_GEOIP_SUBDIR = "geolite2"

# P3TERX mirrors MaxMind's free GeoLite2 releases with no license key or
# account required. github.com's /raw/ path always 302s to
# raw.githubusercontent.com before serving the file, so the redirect target
# must be allowed too - only these hosts may ever be contacted or redirected
# to, see _validate_download_url().
_ALLOWED_DOWNLOAD_HOSTS = frozenset({"github.com", "raw.githubusercontent.com"})
_CITY_DOWNLOAD_URL = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-City.mmdb"
_ASN_DOWNLOAD_URL = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-ASN.mmdb"

# Production sizes are roughly City ~60 MB, ASN ~8 MB; the floors below catch
# a truncated or replaced (e.g. HTML error page) download without being
# fragile against normal edition-to-edition size drift. The hard cap is a
# safety limit against a compromised or misconfigured mirror, not a realistic
# expectation.
_MIN_CITY_SIZE = 10_000_000
_MIN_ASN_SIZE = 1_000_000
_MAX_DOWNLOAD_SIZE = 200_000_000
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_LRU_CACHE_SIZE = 4096
# P3TERX rebuilds its mirror roughly daily; a local file older than this is
# considered stale even though it is still a structurally valid mmdb, so the
# refresh daemon (app/main.py::_geoip_database_daemon) actually re-downloads
# instead of finding a "valid" file forever and never fetching again.
_STALE_AFTER_SECONDS = 86_400
# Used only to force a real trie read during verification, never logged or
# attributed to a request; any public address would do.
_SMOKE_TEST_IP = "8.8.8.8"

_download_lock = RLock()


@dataclass(frozen=True, slots=True)
class _DBSpec:
    label: str
    filename: str
    url: str
    min_size: int
    expected_type: str


_CITY_SPEC = _DBSpec("City", "GeoLite2-City.mmdb", _CITY_DOWNLOAD_URL, _MIN_CITY_SIZE, "City")
_ASN_SPEC = _DBSpec("ASN", "GeoLite2-ASN.mmdb", _ASN_DOWNLOAD_URL, _MIN_ASN_SIZE, "ASN")


class IPInfo(TypedDict, total=False):
    country: str | None
    city: str | None
    asn: int | None
    as_org: str | None


class RequesterInfo(TypedDict, total=False):
    ip: Required[str]
    country: str
    city: str
    asn: int
    as_org: str


def _geoip_dir() -> Path:
    directory = get_data_dir() / _GEOIP_SUBDIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _db_path(spec: _DBSpec) -> Path:
    return _geoip_dir() / spec.filename


def _public_ip(ip: str) -> bool:
    """True when *ip* is a valid, globally routable address (IPv4 or IPv6)."""
    try:
        return ip_address(ip.strip()).is_global
    except ValueError:
        return False


class _ReaderManager:
    """Thread-safe, lazily-initialized wrapper around a geoip2 mmdb reader."""

    def __init__(self, spec: _DBSpec) -> None:
        self._spec = spec
        self._reader: geoip2.database.Reader | None = None
        self._lock = RLock()
        self._missing_logged = False

    def get(self) -> geoip2.database.Reader | None:
        if not _HAS_GEOIP2:
            return None
        if self._reader is not None:
            return self._reader
        with self._lock:
            if self._reader is not None:
                return self._reader
            path = _db_path(self._spec)
            if not path.exists():
                if not self._missing_logged:
                    self._missing_logged = True
                    logger.info("GeoIP %s database not present at %s; lookups disabled", self._spec.label, path)
                return None
            try:
                self._reader = geoip2.database.Reader(str(path))
            except Exception:
                logger.warning("Failed to open GeoIP %s database at %s", self._spec.label, path, exc_info=True)
                return None
            return self._reader

    @contextlib.contextmanager
    def use(self):
        """Yield the reader for one lookup while holding the lock close() also takes.

        Guarantees a lookup never sees a reader torn down mid-use by the daily
        refresh (close_readers()): the two block on the same RLock instead of
        racing.
        """
        with self._lock:
            yield self.get()

    def close(self) -> None:
        with self._lock:
            if self._reader is not None:
                with contextlib.suppress(Exception):
                    self._reader.close()
                self._reader = None
                self._missing_logged = False


_city_reader = _ReaderManager(_CITY_SPEC)
_asn_reader = _ReaderManager(_ASN_SPEC)
# Bumped whenever a fresh database replaces the readers, so stale lru_cache
# entries from before a download cannot outlive the reader that produced them.
_cache_generation = 0


@lru_cache(maxsize=_LRU_CACHE_SIZE)
def _cached_lookup(ip: str, generation: int) -> IPInfo | None:
    _ = generation  # part of the cache key only; see close_readers()
    country: str | None = None
    city: str | None = None
    with _city_reader.use() as reader:
        if reader is not None:
            try:
                result = reader.city(ip)
                country = result.country.iso_code
                city = result.city.name
            except (geoip2.errors.AddressNotFoundError, geoip2.errors.GeoIP2Error):
                pass

    asn: int | None = None
    as_org: str | None = None
    with _asn_reader.use() as asn_reader:
        if asn_reader is not None:
            try:
                asn_result = asn_reader.asn(ip)
                asn = asn_result.autonomous_system_number
                as_org = asn_result.autonomous_system_organization
            except (geoip2.errors.AddressNotFoundError, geoip2.errors.GeoIP2Error):
                pass

    if country is None and city is None and asn is None and as_org is None:
        return None
    return IPInfo(country=country, city=city, asn=asn, as_org=as_org)


def lookup_ip(ip: str) -> IPInfo | None:
    """Resolve *ip* to country/city/ASN, or None if unresolvable.

    Returns None for private/reserved addresses, when neither database is
    present, or when the address has no record in either one.
    """
    if not _public_ip(ip):
        return None
    return _cached_lookup(ip, _cache_generation)


def build_requester_info(client_ip: str | None) -> RequesterInfo | None:
    """Build the "requester" tile data for a job, or None to omit it.

    Shared by the job detail page (app/routes/media.py) and the single-job
    API endpoint (app/routes/api.py) - never the bulk job list or the public
    share view. IP is displayed in its normalized/compressed form (works for
    both IPv4 and IPv6); country/city/ASN are resolved fresh on every call
    rather than persisted, so an omission here (private address, no GeoIP
    database yet, no match) never surfaces as an error - the affected piece
    is simply left out.
    """
    if not client_ip:
        return None
    try:
        display_ip = str(ip_address(client_ip.strip()))
    except ValueError:
        return None

    info: RequesterInfo = {"ip": display_ip}
    geo = lookup_ip(client_ip)
    if geo:
        if geo.get("country"):
            info["country"] = geo["country"]
        if geo.get("city"):
            info["city"] = geo["city"]
        if geo.get("asn"):
            info["asn"] = geo["asn"]
        if geo.get("as_org"):
            info["as_org"] = geo["as_org"]
    return info


def close_readers() -> None:
    """Close open readers and invalidate the lookup cache (after a download)."""
    global _cache_generation
    _cache_generation += 1
    _city_reader.close()
    _asn_reader.close()
    _cached_lookup.cache_clear()


def _validate_download_url(url: str) -> None:
    """Restrict downloads (including redirects) to the trusted mirror hosts."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f"Refusing GeoIP download from untrusted URL: {url}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_mmdb(path: Path, spec: _DBSpec) -> bool:
    # P3TERX publishes no checksum/hash file alongside these mirrored .mmdb
    # assets to verify a download against, so integrity is established by
    # fully parsing the file (metadata + a real trie read below) rather than
    # comparing a digest; the sha256 logged in _download_one is for manual
    # cross-checking only, not an automated integrity gate.
    if not path.exists():
        return False
    size = path.stat().st_size
    if size < spec.min_size:
        logger.warning("GeoIP %s database too small (%d bytes); treating as invalid", spec.label, size)
        return False
    if not _HAS_GEOIP2:
        return True
    try:
        with geoip2.database.Reader(str(path)) as reader:
            db_type = reader.metadata().database_type
            if spec.expected_type not in db_type:
                logger.warning("GeoIP %s database type mismatch: got %s", spec.label, db_type)
                return False
            # Metadata alone can parse from a truncated/corrupted body; force
            # one real lookup so a broken data section fails here instead of
            # surfacing as a silently degraded lookup_ip() later.
            try:
                if spec.label == "City":
                    reader.city(_SMOKE_TEST_IP)
                else:
                    reader.asn(_SMOKE_TEST_IP)
            except geoip2.errors.AddressNotFoundError:
                pass
    except Exception:
        logger.warning("GeoIP %s database failed verification", spec.label, exc_info=True)
        return False
    return True


def _is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > _STALE_AFTER_SECONDS


def _download_one(spec: _DBSpec) -> bool:
    """Download *spec* into place if the local copy is missing, invalid or stale.

    Returns True if a new file was written. Best-effort: any failure is
    logged and swallowed so a broken mirror never prevents the app from
    serving requests, and a stale-but-still-valid local file is left in place
    on a failed refresh attempt.
    """
    target = _db_path(spec)
    if _verify_mmdb(target, spec) and not _is_stale(target):
        return False

    _validate_download_url(spec.url)
    fd, tmp_name = tempfile.mkstemp(suffix=".mmdb.tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o644)
        downloaded = 0
        with httpx.Client(follow_redirects=False, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client, os.fdopen(
            fd, "wb"
        ) as handle:
            fd = -1
            next_url = spec.url
            for _hop in range(5):
                _validate_download_url(next_url)
                with client.stream("GET", next_url) as response:
                    if response.is_redirect:
                        next_url = str(response.next_request.url) if response.next_request else ""
                        if not next_url:
                            raise ValueError("GeoIP download redirected without a target URL")
                        continue
                    response.raise_for_status()
                    if "text/html" in response.headers.get("content-type", "").lower():
                        raise ValueError("GeoIP download returned an HTML page instead of a database")
                    for chunk in response.iter_bytes(65_536):
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD_SIZE:
                            raise ValueError(f"GeoIP download exceeded the {_MAX_DOWNLOAD_SIZE}-byte safety cap")
                        handle.write(chunk)
                    break
            else:
                raise ValueError("GeoIP download followed too many redirects")
            handle.flush()
            os.fsync(handle.fileno())

        if not _verify_mmdb(tmp_path, spec):
            return False
        checksum = _sha256_file(tmp_path)
        tmp_path.replace(target)
        target.chmod(0o644)
        logger.info("Downloaded GeoIP %s database (%d bytes, sha256=%s)", spec.label, downloaded, checksum)
        return True
    except Exception:
        logger.warning("GeoIP %s database download failed", spec.label, exc_info=True)
        return False
    finally:
        if fd != -1:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()


def ensure_geoip_databases() -> dict[str, bool]:
    """Blocking: download whichever GeoLite2 databases are missing or invalid.

    Must not run on the event loop; use ensure_geoip_databases_async() from
    async code. Safe to call repeatedly - a valid, present database is left
    untouched.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("ensure_geoip_databases() must not run on an active event loop")

    _geoip_dir()  # ensure the directory exists before the download attempt
    # Prevents two threads in this process from racing to replace the same
    # file; fetchly's WORKERS setting is pinned to 1 (see app/AGENTS.md), so a
    # process-local lock is the whole story - no cross-process lock needed.
    with _download_lock:
        city_downloaded = _download_one(_CITY_SPEC)
        asn_downloaded = _download_one(_ASN_SPEC)
        if city_downloaded or asn_downloaded:
            close_readers()
        return {
            "city": _verify_mmdb(_db_path(_CITY_SPEC), _CITY_SPEC),
            "asn": _verify_mmdb(_db_path(_ASN_SPEC), _ASN_SPEC),
        }


async def ensure_geoip_databases_async() -> dict[str, bool]:
    """Async wrapper for ensure_geoip_databases()."""
    return await asyncio.to_thread(ensure_geoip_databases)
