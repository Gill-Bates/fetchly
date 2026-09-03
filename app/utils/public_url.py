#!/usr/bin/env python3
#
# app/utils/public_url.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Operator-configurable public host for outward-facing links.

fetchly hands out share links built from whatever ``Host`` the creating request
arrived with. Behind a reverse proxy that is often an internal name
(``fetchly:8000``, ``10.0.0.5``) that the recipient cannot resolve. An admin can
pin the public hostname in Settings; every share link is then rendered against
it instead.

The stored value is a bare hostname or IP - no scheme, port or path, matching
the FQDN field in the wirebuddy sister project. HTTPS is assumed when it is set:
a public reverse proxy terminates TLS in practically every deployment, and
leaving the field blank keeps the original request-derived behaviour (including
plain HTTP) available.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

# RFC 1035 wire limit. The label loop below enforces the 63-octet per-label cap.
_MAX_HOSTNAME_LENGTH = 253
# Unanchored: applied with fullmatch(), which - unlike match() with a trailing
# "$" - also rejects a trailing newline.
_HOSTNAME_CHARSET_RE = re.compile(r"[A-Za-z0-9.-]+")


def normalize_public_hostname(value: str | None) -> str:
    """Validate and normalise an operator-provided public hostname or IP.

    Returns the cleaned host, or ``""`` when nothing is configured (the caller
    then falls back to the request host). Raises :class:`ValueError` with a
    user-facing message for anything that is neither a bare IP address nor a
    plausible DNS name.
    """
    host = (value or "").strip()
    if not host:
        return ""

    # A bare IPv4/IPv6 address is accepted and returned in canonical form.
    # Brackets are tolerated on input ("[::1]") but never stored.
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass

    if ":" in host:
        raise ValueError("Enter a hostname or IP only - no scheme, port or path")
    if len(host) > _MAX_HOSTNAME_LENGTH:
        raise ValueError("Hostname is too long")
    if not _HOSTNAME_CHARSET_RE.fullmatch(host):
        raise ValueError("Hostname may only contain letters, digits, dots and hyphens")
    if ".." in host or host.startswith(".") or host.endswith("."):
        raise ValueError("Hostname has an empty label")
    for label in host.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise ValueError("Hostname has an invalid label")

    return host


def build_public_base_url(request: Request, public_hostname: str | None) -> str:
    """Return the scheme+host origin that outward-facing links should use.

    With a configured hostname: ``https://<host>`` (IPv6 bracketed). Without
    one: the request's own base URL, which already reflects ``X-Forwarded-*``
    when a trusted proxy sets them (see ProxyHeadersMiddleware in app/main.py).
    """
    host = (public_hostname or "").strip().strip("[]")
    if not host:
        return str(request.base_url).rstrip("/")

    url_host = f"[{host}]" if ":" in host else host
    return f"https://{url_host}"
