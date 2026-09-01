#!/usr/bin/env python3
#
# app/utils/cookie_status.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Structural validity check for one platform's stored cookie jar.

Both ends of the application ask the same question about a cookie file - the
Settings UI ("what should this tile say?", see app/routes/cookies.py) and the
download path ("is it worth handing this to yt-dlp?", see app/worker.py and
app/utils/youtube.py) - so the judgement lives here once instead of in each
caller.

It is deliberately a *structural* check: does the file parse as a Netscape
cookie jar, does it carry cookies for that platform's domain, and are they
unexpired. It is not a live check against the platform - that would need an
actual request per cookie file and would risk tripping the platform's own
abuse detection.

This module knows about platforms but must not import app/utils/platform.py:
that module imports youtube.py, which imports this one for the cookie gate.
The maps below are keyed by the same platform identifiers.
"""

from __future__ import annotations

import http.cookiejar
import logging
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Final

logger = logging.getLogger(__name__)

# Cookie domains expected for each platform's session cookies. This is a
# sanity check that the jar actually belongs to that platform, not an
# exhaustive list of every cookie a browser export may contain (Google auth
# cookies for YouTube are commonly set on google.com, not youtube.com).
PLATFORM_COOKIE_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    "youtube": (".youtube.com", ".google.com"),
    "tiktok": (".tiktok.com",),
    "instagram": (".instagram.com",),
    "facebook": (".facebook.com",),
}

# The domain a cookie is filed under when the source format does not name one
# (a copied `cookie:` request header carries names and values only). A request
# to youtube.com only ever carries youtube.com-scoped cookies, so attributing
# the whole header to the platform's own domain reproduces what the browser
# sent.
PLATFORM_PRIMARY_DOMAIN: Final[dict[str, str]] = {
    "youtube": ".youtube.com",
    "tiktok": ".tiktok.com",
    "instagram": ".instagram.com",
    "facebook": ".facebook.com",
}

# What a jar must carry to count as a signed-in session, as groups: one
# cookie from every group has to be present. They are all HttpOnly, so their
# absence is also the reliable signal that an import came from
# `document.cookie` in the console, which cannot see them.
#
# The YouTube entry mirrors yt-dlp's own test rather than guessing
# (YoutubeBaseInfoExtractor._has_auth_cookies): LOGIN_INFO *and* one of the
# SAPISID family. LOGIN_INFO matters because it is the one YouTube clears on
# rotation, and it only exists on youtube.com - a jar copied from a
# google.com request passes a naive "has a SID cookie" check and is then
# treated as signed out by the downloader.
PLATFORM_REQUIRED_COOKIES: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "youtube": (
        ("LOGIN_INFO",),
        ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"),
    ),
    # yt-dlp's TikTok extractor authenticates with sid_tt specifically
    # (TikTokBaseIE._real_initialize and its format handling), not sessionid -
    # TikTok sets them together, so requiring the one that is read is free.
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
        """Whether the file parses and still holds unexpired platform cookies.

        This is a structural judgement only - it says the jar is readable and
        current, not that anyone is signed in.
        """
        return self.status == "valid"

    @property
    def is_authenticated(self) -> bool:
        """Whether this jar should be handed to yt-dlp at all.

        Structural validity is not enough. A jar without the platform's login
        cookies authenticates nothing, and an expired or malformed one is
        worse than none: yt-dlp loads cookie files with ignore_expires=True,
        so a dead session would still be sent, and platforms answer a stale
        or half-present login less kindly than an anonymous request. Both
        cases fall back to the anonymous path the app takes with no file at
        all - which is what the Settings tile promises.
        """
        return self.is_usable and not self.missing_login_cookies


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
    """Return a cookie's expiry, or None when it is a session cookie.

    The Netscape format has no dedicated marker for session cookies, so yt-dlp
    writes 0 and maps a stored 0 back to "no expiry" when loading
    (YoutubeDLCookieJar.load). MozillaCookieJar takes the same field literally
    and yields the timestamp 0, i.e. 1970 - which would make every session
    cookie in a jar look long expired. Mirroring yt-dlp here keeps the status
    shown in Settings and the jar yt-dlp actually sees in agreement.
    """
    expires = cookie.expires
    if expires is None or expires == 0:
        return None
    return expires


def missing_login_cookies(names: tuple[str, ...] | list[str], platform: str) -> tuple[str, ...]:
    """Which login cookie is absent, one name per unsatisfied group.

    Empty when the jar carries a usable session. The name returned for a group
    is its first (canonical) member, which is what the user should go looking
    for.
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
    """When the jar was last written, taken from the file itself.

    That is the import for a fresh jar - but yt-dlp rewrites the cookie file
    it was given at the end of every run (YoutubeDL.save_cookies, reached
    through its context manager), so after the first download this is the
    moment the platform last rotated these cookies. That makes it the more
    useful of the two: an expiry date says when the cookies formally lapse,
    while this says when the session was last demonstrably alive.

    The rewrite truncates the existing file rather than recreating it, so the
    owner-only permissions this app sets survive it.
    """
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return None


def analyze_cookie_file(path: Path, platform: str) -> CookieAnalysis:
    """Parse a Netscape cookie file and judge whether it looks usable.

    Runs synchronously (http.cookiejar does blocking file I/O); async callers
    use asyncio.to_thread() to keep this off the event loop.
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
    """Whether ``path`` holds cookies worth passing to yt-dlp for ``platform``.

    Never raises: a jar that cannot be read at all is simply not usable, and
    the download continues anonymously the same way it does without a file.
    """
    try:
        return analyze_cookie_file(path, platform).is_authenticated
    except OSError as exc:
        logger.warning("Could not read cookie file %s for %s: %s", path, platform, exc)
        return False
