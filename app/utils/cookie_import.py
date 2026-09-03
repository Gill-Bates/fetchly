#!/usr/bin/env python3
#
# app/utils/cookie_import.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Turn whatever the browser's dev tools hand out into a Netscape cookie jar.

yt-dlp only reads Netscape-format cookie files, but nothing in a browser
offers that format by hand: what a user can actually copy out of the dev
tools is the `cookie:` request header, one of the "Copy as ..." commands from
the Network tab's context menu, or a JSON export from a cookie extension.
This module accepts all of those and normalizes them, so Settings can offer a
paste box instead of demanding a prepared file (see app/routes/cookies.py).

Recognized inputs:

* ``header``   - ``name=value; name2=value2``, with or without a leading
  ``Cookie:``. This is the Network tab's "Copy value" on the cookie request
  header, and the format everything else is reduced to.
* ``request``  - a whole "Copy as cURL / fetch / PowerShell" command from the
  Network tab, in any of the shells the menu offers (bash quoting, cmd caret
  escapes, PowerShell backticks); the cookies are lifted out of the header,
  or out of the session object PowerShell seeds instead of one.
* ``json``     - the array written by cookie-export extensions
  (Cookie-Editor, EditThisCookie), which is the only input that carries real
  per-cookie expiry, domain and flags.
* ``netscape`` - an already-prepared file, passed through unchanged apart
  from normalization.

Every format that names its own domains (json, netscape, the PowerShell
session object) is filtered to the platform's domains before anything is
stored, so a whole-browser export cannot deposit unrelated sites' session
tokens on the data volume.

A copied header carries names and values but no domain, path, flags or
expiry, so those are reconstructed: the platform's own domain (a request to
youtube.com only ever carries youtube.com-scoped cookies), path ``/``, the
secure flag (all four platforms are HTTPS-only, and ``__Secure-``/``__Host-``
prefixed names are invalid without it) and expiry ``0`` - yt-dlp's marker for
a session cookie, which it sends but never treats as stale. Guessing a
plausible future timestamp instead would put a fabricated expiry date in
front of the user in Settings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

from .cookie_status import (
    PLATFORM_COOKIE_DOMAINS,
    PLATFORM_PRIMARY_DOMAIN,
    PLATFORM_REQUIRED_COOKIES,
    domain_matches,
    missing_login_cookies,
)

_NETSCAPE_HEADER: Final = "# Netscape HTTP Cookie File\n# Written by fetchly - do not edit.\n\n"

# yt-dlp's own prefix for HttpOnly entries. It is stripped on import: plain
# http.cookiejar (which every validity check in this app goes through) reads
# those lines as comments and would judge a perfectly good jar as empty.
_HTTPONLY_PREFIX: Final = "#HttpOnly_"

# RFC 6265 token characters - anything else in a name means the input was not
# a cookie header at all.
_COOKIE_NAME_RE: Final = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# A Netscape entry is seven tab-separated fields.
_NETSCAPE_FIELDS: Final = 7

# Generous upper bound; a real browser jar for one site holds a few dozen.
_MAX_COOKIES: Final = 512

# The cookie header as it appears in the Network tab's "Copy as ..." output:
# cURL (bash and cmd), fetch/Node.js, and PowerShell all quote it differently.
# The request's own URL, as each "Copy as ..." flavour writes it.
_REQUEST_URL_PATTERNS: Final = (
    re.compile(r"""(?:--url|-Uri)\s+(['"])(?P<url>https?://[^'"\s]+)\1""", re.I),
    re.compile(r"""fetch\(\s*(['"])(?P<url>https?://[^'"\s]+)\1""", re.I),
    re.compile(r"""^\s*curl\s+(['"])(?P<url>https?://[^'"\s]+)\1""", re.I),
)

_CURL_COOKIE_PATTERNS: Final = (
    re.compile(r"""(?:-H|--header)\s+(['"])\s*cookie\s*:\s*(?P<value>.*?)\1""", re.I | re.S),
    re.compile(r"""(['"])cookie\1\s*[:=]\s*(['"])(?P<value>.*?)\2""", re.I | re.S),
    re.compile(r"""(?:-b|--cookie)\s+(['"])(?P<value>.*?)\1""", re.S),
)


# Anything that came out of the Network tab's context menu. Recognizing it
# matters for the error message: pasting a request that carries no cookies is
# a different mistake from pasting something that is not a request at all.
_COPIED_COMMAND_RE: Final = re.compile(r"^\s*(?:curl\b|fetch\s*\(|Invoke-WebRequest\b|\$session\b)", re.I)


def _unescape_copied_command(text: str) -> str:
    """Undo the shell quoting Windows puts into a copied request.

    "Copy as cURL (cmd)" escapes every quote, ampersand and percent with a
    caret and continues lines with a trailing one, so the header this module
    looks for arrives as ^"cookie: ...^". PowerShell does the same job with
    backticks. Both are undone here rather than in each pattern below - and
    undoing them is not optional: without it the extractor finds no header,
    the raw command falls through to the plain-header parser, and what gets
    stored is a jar with the first cookie missing and the rest mangled.
    """
    if "^\"" in text:
        # cmd.exe escapes with ^, so ^^ is a literal caret and a trailing ^
        # joins the next line.
        text = re.sub(r"\^\r?\n\s*", " ", text)
        return re.sub(r"\^(.)", r"\1", text, flags=re.S)

    if "`\"" in text:
        text = re.sub(r"`\r?\n\s*", " ", text)
        return text.replace('`"', '"').replace("``", "`")

    return text


# "Copy as PowerShell" does not build a cookie header at all - it seeds a
# WebRequestSession one cookie at a time, in (name, value, path, domain)
# order. Without this the paste looks cookie-free and gets refused.
_POWERSHELL_COOKIE_RE: Final = re.compile(
    r"""New-Object\s+System\.Net\.Cookie\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)""",
    re.I,
)


class CookieImportError(ValueError):
    """Raised when pasted text cannot be read as cookies."""


@dataclass(frozen=True)
class CookieImport:
    """A pasted blob converted to a Netscape jar."""

    netscape: str
    source_format: str  # "header" | "request" | "json" | "netscape"
    names: tuple[str, ...]

    @property
    def cookie_count(self) -> int:
        return len(self.names)


def has_session_cookie(names: tuple[str, ...], platform: str) -> bool:
    """Whether ``names`` carries everything a signed-in session needs."""
    return not missing_login_cookies(names, platform)


def missing_session_cookie_hint(platform: str) -> str:
    """Explain what a login-free paste is missing and where it lives."""
    groups = PLATFORM_REQUIRED_COOKIES.get(platform, ())
    names = ", ".join(group[0] for group in groups) or "a login cookie"
    site = PLATFORM_PRIMARY_DOMAIN.get(platform, "").lstrip(".")
    return (
        f"This paste is missing the login cookies ({names}). They are "
        f"HttpOnly - document.cookie in the console cannot read them - and "
        f"they only exist on {site}, so copy a request that goes there while "
        f"signed in."
    )


def _netscape_line(
    *, domain: str, path: str, secure: bool, expires: int, name: str, value: str
) -> str:
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    return "\t".join(
        (
            domain,
            include_subdomains,
            path or "/",
            "TRUE" if secure else "FALSE",
            str(expires),
            name,
            value,
        )
    )


def _belongs_to(domain: str, platform: str) -> bool:
    """Whether a cookie carrying its own domain may be stored for a platform.

    JSON exports, PowerShell copies and prepared Netscape files each name a
    domain per cookie, and an export can cover the whole browser. Without this
    filter a paste would write every unrelated site's session token into the
    data volume - and a foreign cookie that happens to be called "sessionid"
    would satisfy the login check for Instagram.
    """
    return domain_matches(domain, PLATFORM_COOKIE_DOMAINS.get(platform, ()))


def _no_platform_cookies(platform: str) -> CookieImportError:
    allowed = ", ".join(PLATFORM_COOKIE_DOMAINS.get(platform, ())) or platform
    return CookieImportError(f"None of these cookies belong to {allowed}")


def _is_writable(name: str, value: str) -> bool:
    """Reject entries that a tab-separated format cannot represent."""
    if not name or not _COOKIE_NAME_RE.match(name):
        return False
    return "\t" not in value and "\n" not in value and "\r" not in value


def _looks_like_netscape(text: str) -> bool:
    if text.lstrip().startswith("# Netscape HTTP Cookie File"):
        return True
    return any(
        line.count("\t") == _NETSCAPE_FIELDS - 1
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )


def _passthrough_netscape(text: str, platform: str) -> tuple[str, tuple[str, ...]]:
    lines: list[str] = []
    names: list[str] = []
    seen_entry = False
    for raw_line in text.splitlines():
        line = raw_line[len(_HTTPONLY_PREFIX) :] if raw_line.startswith(_HTTPONLY_PREFIX) else raw_line
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) != _NETSCAPE_FIELDS:
            continue
        seen_entry = True
        if not _belongs_to(fields[0], platform):
            continue
        names.append(fields[5])
        lines.append("\t".join(fields))

    if not lines:
        if seen_entry:
            raise _no_platform_cookies(platform)
        raise CookieImportError("No cookie entries found in this Netscape cookie file")
    return _NETSCAPE_HEADER + "\n".join(lines) + "\n", tuple(names)


def _request_url(text: str) -> str | None:
    """The URL a copied request was aimed at, if the command names one."""
    for pattern in _REQUEST_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("url")
    return None


def _reject_foreign_request(text: str, platform: str) -> None:
    """Refuse a request copied from someone else's domain.

    A page embeds third-party requests (Google's scripts on YouTube, a CDN,
    an analytics beacon), and those carry that domain's cookies - which look
    close enough to pass a name check while missing the first-party cookies
    the downloader needs. Filing them under the platform's domain would
    produce a jar that Settings calls valid and yt-dlp treats as signed out.
    """
    url = _request_url(text)
    if not url:
        return

    host = urlparse(url).hostname or ""
    expected = PLATFORM_PRIMARY_DOMAIN.get(platform, "")
    if not host or not expected or domain_matches(host, (expected,)):
        return

    raise CookieImportError(
        f"This request went to {host}, so it carries that site's cookies, not "
        f"{expected.lstrip('.')}'s. Copy a request whose domain column reads "
        f"{expected.lstrip('.')}."
    )


def _cookie_header_from_curl(text: str) -> str | None:
    for pattern in _CURL_COOKIE_PATTERNS:
        match = pattern.search(text)
        if match:
            candidate = match.group("value").strip()
            if "=" in candidate:
                return candidate
    return None


def _pairs_from_header(header: str) -> list[tuple[str, str]]:
    """Split a ``name=value; name2=value2`` header into its pairs."""
    cleaned = re.sub(r"^\s*cookie\s*:\s*", "", header, flags=re.I)
    pairs: list[tuple[str, str]] = []
    for chunk in cleaned.split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if _is_writable(name, value):
            pairs.append((name, value))
    return pairs


def _from_header(header: str, platform: str, source_format: str) -> CookieImport:
    pairs = _pairs_from_header(header)
    if not pairs:
        raise CookieImportError(
            "No cookies found. Expected the cookie request header, e.g. "
            "'SID=abc; HSID=def'."
        )
    if len(pairs) > _MAX_COOKIES:
        raise CookieImportError("Too many cookies in one paste")

    domain = PLATFORM_PRIMARY_DOMAIN.get(platform, "")
    if not domain:
        raise CookieImportError("Unknown platform")

    lines = [
        _netscape_line(
            domain=domain, path="/", secure=True, expires=0, name=name, value=value
        )
        for name, value in pairs
    ]
    return CookieImport(
        netscape=_NETSCAPE_HEADER + "\n".join(lines) + "\n",
        source_format=source_format,
        names=tuple(name for name, _ in pairs),
    )


def _from_powershell(text: str, platform: str) -> CookieImport | None:
    """Read the cookies a PowerShell copy adds to its session object."""
    matches = _POWERSHELL_COOKIE_RE.findall(text)
    if not matches:
        return None
    if len(matches) > _MAX_COOKIES:
        raise CookieImportError("Too many cookies in one paste")

    fallback_domain = PLATFORM_PRIMARY_DOMAIN.get(platform, "")
    if not fallback_domain:
        raise CookieImportError("Unknown platform")

    lines: list[str] = []
    names: list[str] = []
    seen_entry = False
    for name, value, path, domain in matches:
        if not _is_writable(name, value):
            continue
        seen_entry = True
        if not _belongs_to(domain.strip() or fallback_domain, platform):
            continue
        lines.append(
            _netscape_line(
                domain=domain.strip() or fallback_domain,
                path=path.strip() or "/",
                secure=True,
                expires=0,
                name=name,
                value=value,
            )
        )
        names.append(name)

    if not lines:
        if seen_entry:
            raise _no_platform_cookies(platform)
        raise CookieImportError("The PowerShell command contains no usable cookies")

    return CookieImport(
        netscape=_NETSCAPE_HEADER + "\n".join(lines) + "\n",
        source_format="request",
        names=tuple(names),
    )


def _expiry_from_json(entry: dict[str, Any]) -> int:
    """Read an extension export's expiry, falling back to a session cookie."""
    if entry.get("session") is True:
        return 0
    for key in ("expirationDate", "expires", "expiry", "expires_utc"):
        raw = entry.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            expires = int(float(raw))
        except (TypeError, ValueError):
            continue
        return expires if expires > 0 else 0
    return 0


def _from_json(text: str, platform: str) -> CookieImport:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise CookieImportError("This looks like JSON but could not be parsed") from exc

    if isinstance(payload, dict):
        payload = payload.get("cookies", payload.get("Cookies"))
    if not isinstance(payload, list):
        raise CookieImportError("Expected a JSON array of cookies, as exported by cookie extensions")

    fallback_domain = PLATFORM_PRIMARY_DOMAIN.get(platform, "")
    if not fallback_domain:
        raise CookieImportError("Unknown platform")

    lines: list[str] = []
    names: list[str] = []
    seen_entry = False
    for entry in payload[: _MAX_COOKIES + 1]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        value = str(entry.get("value") or "")
        if not _is_writable(name, value):
            continue

        domain = str(entry.get("domain") or fallback_domain).strip() or fallback_domain
        # A host-only cookie is stored without the leading dot, a domain
        # cookie with it; extensions disagree on which field carries that.
        if entry.get("hostOnly") is True:
            domain = domain.lstrip(".")
        elif not domain.startswith(".") and entry.get("hostOnly") is False:
            domain = "." + domain

        seen_entry = True
        if not _belongs_to(domain, platform):
            continue

        lines.append(
            _netscape_line(
                domain=domain,
                path=str(entry.get("path") or "/"),
                secure=bool(entry.get("secure", True)),
                expires=_expiry_from_json(entry),
                name=name,
                value=value,
            )
        )
        names.append(name)

    if not lines:
        if seen_entry:
            raise _no_platform_cookies(platform)
        raise CookieImportError("The JSON contains no usable cookies")
    if len(lines) > _MAX_COOKIES:
        raise CookieImportError("Too many cookies in one paste")

    return CookieImport(
        netscape=_NETSCAPE_HEADER + "\n".join(lines) + "\n",
        source_format="json",
        names=tuple(names),
    )


def convert_to_netscape(text: str, platform: str) -> CookieImport:
    """Convert pasted cookie text of any recognized shape to a Netscape jar.

    Raises CookieImportError with a message meant for the user when the input
    cannot be read as cookies at all.
    """
    if not text or not text.strip():
        raise CookieImportError("Nothing pasted")

    stripped = text.strip()

    if stripped[0] in "[{":
        return _from_json(stripped, platform)

    if _looks_like_netscape(text):
        netscape, names = _passthrough_netscape(text, platform)
        return CookieImport(netscape=netscape, source_format="netscape", names=names)

    unescaped = _unescape_copied_command(stripped)
    if _COPIED_COMMAND_RE.match(unescaped):
        _reject_foreign_request(unescaped, platform)

    header = _cookie_header_from_curl(unescaped)
    if header is not None:
        return _from_header(header, platform, "request")

    from_powershell = _from_powershell(unescaped, platform)
    if from_powershell is not None:
        return from_powershell

    # A copied request with no cookie header in it must never fall through to
    # the parser below: the command line is full of "name=value" pairs, and
    # picking those up would store a jar that is part URL and part header.
    if _COPIED_COMMAND_RE.match(unescaped):
        site = PLATFORM_PRIMARY_DOMAIN.get(platform, "").lstrip(".")
        raise CookieImportError(
            f"This request carries no cookies. A page answered from the cache "
            f"or a service worker only ever shows provisional headers - pick a "
            f"Fetch/XHR request to {site} instead, not the page request itself "
            f"and not a call to another domain."
        )

    # A single line of "name=value; ..." - and the fallback for anything else,
    # so the error the user sees names the format that was expected.
    return _from_header(stripped.replace("\n", " "), platform, "header")
