#!/usr/bin/env python3
#
# app/utils/cookie_import.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Turn whatever the browser's dev tools hand out into a Netscape cookie jar.

yt-dlp only reads Netscape files, which no browser exports by hand. This
module normalizes what a user can actually copy, so Settings offers a paste
box instead of demanding a prepared file (see app/routes/cookies.py).

Recognized inputs:

* ``header``   - ``name=value; name2=value2`` (the Network tab's "Copy value"
  on the cookie header); everything else is reduced to this.
* ``request``  - a whole "Copy as cURL / fetch / PowerShell" command (any
  shell); cookies are lifted from its header or PowerShell session object.
* ``json``     - a cookie-extension export array (the only input with real
  per-cookie expiry, domain and flags).
* ``netscape`` - a prepared file, passed through with normalization.

Every format that names its own domains is filtered to the platform's domains
first, so a whole-browser export cannot store unrelated sites' tokens.

A copied header has no domain/path/flags/expiry, so those are reconstructed:
the platform's domain, path ``/``, secure=True (the platforms are HTTPS-only
and ``__Secure-``/``__Host-`` names require it) and expiry ``0`` - yt-dlp's
session-cookie marker. Inventing a future timestamp would show the user a
fabricated expiry date.
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

# yt-dlp's HttpOnly prefix, stripped on import: plain http.cookiejar (which the
# validity checks use) reads those lines as comments and would see an empty jar.
_HTTPONLY_PREFIX: Final = "#HttpOnly_"

# RFC 6265 token chars; anything else means the input was not a cookie header.
_COOKIE_NAME_RE: Final = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

_NETSCAPE_FIELDS: Final = 7  # tab-separated fields per entry
_MAX_COOKIES: Final = 512

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


# A copied command from the Network tab. Detected so a cookie-less request
# gets a different error from something that is not a request at all.
_COPIED_COMMAND_RE: Final = re.compile(r"^\s*(?:curl\b|fetch\s*\(|Invoke-WebRequest\b|\$session\b)", re.I)


def _unescape_copied_command(text: str) -> str:
    """Undo Windows shell quoting in a copied request.

    "Copy as cURL (cmd)" caret-escapes quotes/&/% and continues lines with a
    trailing caret; PowerShell uses backticks. Without undoing them the
    extractor finds no header and stores a mangled jar.
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


# "Copy as PowerShell" seeds a WebRequestSession one cookie at a time in
# (name, value, path, domain) order rather than building a header.
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
    """Whether a domain-carrying cookie may be stored for a platform.

    A whole-browser export would otherwise deposit every site's tokens, and a
    foreign cookie named "sessionid" would pass Instagram's login check.
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
    """Refuse a request copied from a third-party domain (a CDN or analytics
    beacon on the page): its cookies can pass a name check but are not the
    first-party session yt-dlp needs.
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
        # Host-only cookie: no leading dot; domain cookie: leading dot.
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

    # A cookie-less copied request must not fall through: its "name=value"
    # command-line pairs would be stored as a bogus jar.
    if _COPIED_COMMAND_RE.match(unescaped):
        site = PLATFORM_PRIMARY_DOMAIN.get(platform, "").lstrip(".")
        raise CookieImportError(
            f"This request carries no cookies. A page answered from the cache "
            f"or a service worker only ever shows provisional headers - pick a "
            f"Fetch/XHR request to {site} instead, not the page request itself "
            f"and not a call to another domain."
        )

    # Plain "name=value; ..." - and the fallback for anything unrecognized.
    return _from_header(stripped.replace("\n", " "), platform, "header")
