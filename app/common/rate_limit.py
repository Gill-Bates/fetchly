#!/usr/bin/env python3
#
# app/common/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared trusted-proxy and client-IP handling for rate limits."""

from __future__ import annotations

import ipaddress
import os
from functools import cache
from typing import Final

from fastapi import Request
from slowapi import Limiter

type TrustedProxySpec = ipaddress.IPv4Network | ipaddress.IPv6Network

_DEFAULT_TRUSTED_PROXY_IPS: Final = ",".join((
    "127.0.0.1",
    "::1",
))
_RAW_CLIENT_SCOPE_KEY: Final = "fetchly.original_client"


def get_trusted_proxy_hosts() -> str:
    """Raw ``FORWARDED_ALLOW_IPS`` (shared with the proxy middleware)."""
    value = str(os.environ.get("FORWARDED_ALLOW_IPS", _DEFAULT_TRUSTED_PROXY_IPS)).strip()
    return value or _DEFAULT_TRUSTED_PROXY_IPS


def _parse_trusted_proxy_specs(raw_hosts: str) -> tuple[TrustedProxySpec, ...]:
    specs: list[TrustedProxySpec] = []
    for raw_item in raw_hosts.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if item == "*":
            raise RuntimeError("Wildcard trusted proxies are not allowed for client-IP rate limiting")
        try:
            specs.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise RuntimeError(f"Invalid trusted proxy specification: {item!r}") from exc

    if not specs:
        raise RuntimeError("At least one trusted proxy IP or network is required")
    return tuple(specs)


def validate_trusted_proxy_hosts(raw_hosts: str | None = None) -> str:
    """Validate and canonicalize trusted proxy networks for proxy middleware."""
    specs = _parse_trusted_proxy_specs(get_trusted_proxy_hosts() if raw_hosts is None else raw_hosts)
    return ",".join(str(spec) for spec in specs)


@cache
def _trusted_proxy_specs() -> tuple[TrustedProxySpec, ...]:
    """Parse and cache trusted proxy networks for the process lifetime."""
    return _parse_trusted_proxy_specs(get_trusted_proxy_hosts())


def _normalize_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().strip('"')
    if not candidate or candidate.lower() == "unknown":
        return None

    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1:candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _request_peer_host(request: Request) -> str | None:
    """Return the socket peer saved before proxy headers are applied."""
    original_client = request.scope.get(_RAW_CLIENT_SCOPE_KEY)
    if isinstance(original_client, (tuple, list)) and original_client:
        peer_host = original_client[0]
        return peer_host if isinstance(peer_host, str) else None
    return request.client.host if request.client else None


def _is_trusted_proxy(host: str | None) -> bool:
    normalized = _normalize_ip(host)
    if normalized is None:
        return False

    address = ipaddress.ip_address(normalized)
    return any(address in spec for spec in _trusted_proxy_specs())


def _forwarded_client_ip(request: Request) -> str | None:
    peer_host = _request_peer_host(request)
    if not _is_trusted_proxy(peer_host):
        return None

    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        normalized_parts: list[str] = []
        for raw_part in forwarded_for.split(","):
            normalized = _normalize_ip(raw_part)
            if normalized is None:
                # A malformed entry must not become an attacker-controlled
                # rate-limit key; the caller falls back to the socket peer.
                return None
            normalized_parts.append(normalized)

        for forwarded_ip in reversed(normalized_parts):
            if not _is_trusted_proxy(forwarded_ip):
                return forwarded_ip
        return normalized_parts[0]

    return _normalize_ip(request.headers.get("x-real-ip"))


def get_rate_limit_ip(request: Request) -> str:
    """Resolve a stable client IP for SlowAPI behind trusted proxies."""
    forwarded = _forwarded_client_ip(request)
    if forwarded is not None:
        return forwarded

    return _normalize_ip(_request_peer_host(request)) or "unknown"


limiter = Limiter(key_func=get_rate_limit_ip)
