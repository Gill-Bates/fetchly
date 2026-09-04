#!/usr/bin/env python3
#
# app/utils/cookie_status.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Structural validity check for one platform's stored cookie jar.

Shared by the Settings UI and the download path. Structural only: does the
file parse, does it carry unexpired cookies for the platform's domain. Not a
live check - that would need a request per file and risk abuse detection.

Must not import app/utils/platform.py (that imports youtube.py, which imports
this one); the maps below use the same platform identifiers.
"""

from __future__ import annotations

import http.cookiejar
import logging
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Final

logger = logging.getLogger(__name__)

# Domains that mark a jar as belonging to a platform - a sanity check, not an
# exhaustive list (YouTube auth cookies often live on google.com).
PLATFORM_COOKIE_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    "youtube": (".youtube.com", ".google.com"),
    "tiktok": (".tiktok.com",),
    "instagram": (".instagram.com",),
    "facebook": (".facebook.com",),
}

# The domain a cookie is filed under when the source format names none (a
# copied header). A request to youtube.com carries only youtube.com cookies.
PLATFORM_PRIMARY_DOMAIN: Final[dict[str, str]] = {
    "youtube": ".youtube.com",
    "tiktok": ".tiktok.com",
    "instagram": ".instagram.com",
    "facebook": ".facebook.com",
}

# A signed-in session needs one cookie from every group. All HttpOnly, so
# their absence also flags a `document.cookie` console import. The YouTube
# entry mirrors yt-dlp's own _has_auth_cookies test (LOGIN_INFO + a SAPISID).
PLATFORM_REQUIRED_COOKIES: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "youtube": (
        ("LOGIN_INFO",),
        ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"),
    ),
    # yt-dlp's TikTok extractor reads sid_tt, not sessionid (set together).
    "tiktok": (("sid_tt",),),
    "instagram": (("sessionid",),),
    "facebook": (("c_user",), ("xs",)),
}


@dataclass(frozen=True)
class CookieAnalysis:
    """What a stored cookie file looks like right now."""

    platform: str
    present: bool
    status: str  # "valid" | "expired" | "invalid" | "missing"
    cookie_count: int = 0
    matching_domain_count: int = 0
    expires_at: int | None = None
    updated_at: int | None = None
    domains: tuple[str, ...] = ()
    missing_login_cookies: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        """Structural only: the jar parses and holds unexpired platform cookies
        (not that anyone is signed in).
        """
        return self.status == "valid"

    @property
    def is_authenticated(self) -> bool:
        """Whether to hand this jar to yt-dlp at all.

        Needs structural validity *and* the login cookies: yt-dlp sends an
        expired jar anyway (ignore_expires=True) and a stale login is answered
        worse than an anonymous request.
        """
        return self.is_usable and not self.missing_login_cookies

    @property
    def needs_update(self) -> bool:
        """Whether the Settings UI should prompt to replace this jar.

        True for "expired"/"invalid", and also for a structurally "valid" jar
        that is missing its login cookies (present but not actually signed
        in). Single source for this decision: the Settings page (server-
        rendered) and its client-side re-render both read this field instead
        of each re-deriving the same rule from status + is_authenticated.
        """
        return self.status in ("expired", "invalid") or (self.status == "valid" and not self.is_authenticated)


def domain_matches(domain: str, allowed: tuple[str, ...]) -> bool:
    """Whether ``domain`` is one of ``allowed`` or a subdomain of one."""
    if not domain:
        return False
    normalized = domain.lower().lstrip(".")
    return any(
        normalized == candidate.lstrip(".") or normalized.endswith("." + candidate.lstrip("."))
        for candidate in allowed
    )


def _effective_expiry(cookie: http.cookiejar.Cookie) -> int | None:
    """Return a cookie's expiry, or None for a session cookie.

    Netscape has no session marker, so yt-dlp writes 0 and reads 0 back as "no
    expiry". MozillaCookieJar reads 0 as the 1970 timestamp, which would make
    every session cookie look expired - mirror yt-dlp instead.
    """
    expires = cookie.expires
    if expires is None or expires == 0:
        return None
    return expires


def missing_login_cookies(names: tuple[str, ...] | list[str], platform: str) -> tuple[str, ...]:
    """The absent login cookies, one canonical name per unsatisfied group;
    empty when the jar carries a usable session.
    """
    present = set(names)
    return tuple(
        group[0] for group in PLATFORM_REQUIRED_COOKIES.get(platform, ()) if not present & set(group)
    )


def _is_unexpired(cookie: http.cookiejar.Cookie, now: int) -> bool:
    expiry = _effective_expiry(cookie)
    return expiry is None or expiry > now


def _earliest_expiry(cookies: list[http.cookiejar.Cookie]) -> int | None:
    """The soonest real expiry among ``cookies``; None if all are session ones."""
    expiries = [e for e in (_effective_expiry(c) for c in cookies) if e is not None]
    return min(expiries) if expiries else None


def _updated_at(path: Path) -> int | None:
    """The jar's mtime. yt-dlp rewrites the cookie file after every run, so
    after the first download this is when the session was last demonstrably
    alive - more useful than the formal expiry date. (The rewrite truncates
    in place, so owner-only permissions survive.)
    """
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return None


def analyze_cookie_file(path: Path, platform: str) -> CookieAnalysis:
    """Parse a Netscape cookie file and judge whether it looks usable.

    Blocking I/O; async callers use asyncio.to_thread().
    """
    if not path.is_file():
        return CookieAnalysis(platform=platform, present=False, status="missing")

    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        logger.warning("Cookie file for %s failed to parse (%s): %s", platform, path, exc)
        return CookieAnalysis(
            platform=platform,
            present=True,
            status="invalid",
            detail="File is not a valid Netscape-format cookie file",
        )

    all_cookies = list(jar)
    if not all_cookies:
        return CookieAnalysis(
            platform=platform,
            present=True,
            status="invalid",
            cookie_count=0,
            detail="File contains no cookies",
        )

    allowed_domains = PLATFORM_COOKIE_DOMAINS.get(platform, ())
    matching = [c for c in all_cookies if domain_matches(c.domain, allowed_domains)]
    if not matching:
        return CookieAnalysis(
            platform=platform,
            present=True,
            status="invalid",
            cookie_count=len(all_cookies),
            detail=f"No cookies found for {platform}'s domain",
        )

    now = int(time())
    matching_valid = [c for c in matching if _is_unexpired(c, now)]
    if not matching_valid:
        earliest = _earliest_expiry(matching)
        return CookieAnalysis(
            platform=platform,
            present=True,
            status="expired",
            cookie_count=len(all_cookies),
            matching_domain_count=len(matching),
            expires_at=earliest,
            updated_at=_updated_at(path),
            domains=tuple(sorted({c.domain for c in matching})),
            detail="All session cookies for this platform have expired",
        )

    earliest_valid = _earliest_expiry(matching_valid)
    return CookieAnalysis(
        platform=platform,
        present=True,
        status="valid",
        cookie_count=len(all_cookies),
        matching_domain_count=len(matching_valid),
        expires_at=earliest_valid,
        updated_at=_updated_at(path),
        domains=tuple(sorted({c.domain for c in matching_valid})),
        missing_login_cookies=missing_login_cookies([c.name for c in matching_valid], platform),
    )


def cookie_file_is_usable(path: Path, platform: str) -> bool:
    """Whether ``path`` holds cookies worth passing to yt-dlp. Never raises: an
    unreadable jar is just not usable, and the download continues anonymously.
    """
    try:
        return analyze_cookie_file(path, platform).is_authenticated
    except OSError as exc:
        logger.warning("Could not read cookie file %s for %s: %s", path, platform, exc)
        return False
