#!/usr/bin/env python3
#
# app/utils/public_url.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Operator-configurable public host for outward-facing links.

Share links are built from the creating request's ``Host``, which behind a
reverse proxy is often an internal name the recipient cannot resolve. An admin
can pin a bare hostname/IP (no scheme/port/path) in Settings; HTTPS is then
assumed. Blank keeps the request-derived behaviour.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

_MAX_HOSTNAME_LENGTH = 253  # RFC 1035; per-label 63 cap enforced below
# Unanchored: fullmatch() also rejects a trailing newline (unlike match() + "$").
_HOSTNAME_CHARSET_RE = re.compile(r"[A-Za-z0-9.-]+")


def normalize_public_hostname(value: str | None) -> str:
    """Validate an operator-provided public hostname or IP.

    Returns the cleaned host, or ``""`` when unset. Raises ValueError with a
    user-facing message for anything that is not a bare IP or DNS name.
    """
    host = (value or "").strip()
    if not host:
        return ""

    # Bare IPv4/IPv6, returned canonical; input brackets tolerated, not stored.
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
    """The scheme+host origin for outward-facing links.

    Configured hostname: ``https://<host>`` (IPv6 bracketed). Otherwise the
    request's own base URL (already reflects trusted ``X-Forwarded-*``).
    """
    host = (public_hostname or "").strip().strip("[]")
    if not host:
        return str(request.base_url).rstrip("/")

    url_host = f"[{host}]" if ":" in host else host
    return f"https://{url_host}"
