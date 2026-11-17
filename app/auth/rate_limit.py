#!/usr/bin/env python3
#
# app/auth/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared trusted-proxy and client-IP handling for rate limits."""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import threading
import time
import weakref
from collections import OrderedDict, deque
from collections.abc import Callable
from functools import cache
from typing import Final

from fastapi import HTTPException, Request

type TrustedProxySpec = ipaddress.IPv4Network | ipaddress.IPv6Network

_DEFAULT_TRUSTED_PROXY_IPS: Final = "127.0.0.1,::1"
_RAW_CLIENT_SCOPE_KEY: Final = "fetchly.original_client"

logger = logging.getLogger(__name__)

# Bounds limiter memory per route; expired keys go first, then the least recently used.
MAX_KEYS: Final = 10_000
_SPEC_RE: Final = re.compile(r"(\d+)/(second|minute|hour)")
_UNIT_SECONDS: Final = {"second": 1.0, "minute": 60.0, "hour": 3600.0}
_RATE_LIMIT_DETAIL: Final = "Rate limit exceeded"


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

    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing < 0:
            return None
        # Only an optional ``:port`` may follow the bracket; anything else means
        # a malformed entry, which must not be silently reduced to a valid IP.
        remainder = candidate[closing + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return None
        candidate = candidate[1:closing]
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
    """Resolve a stable client IP for rate limiting behind trusted proxies."""
    forwarded = _forwarded_client_ip(request)
    if forwarded is not None:
        return forwarded

    return _normalize_ip(_request_peer_host(request)) or "unknown"


def _bucket_key(client_ip: str) -> str:
    """Collapse an IPv6 client to its /64 so address rotation shares one bucket.

    A /64 is the smallest normal IPv6 subnet handed to one subscriber, so
    rotating addresses inside it must not mint fresh rate-limit buckets.
    IPv4-mapped IPv6 is treated as the IPv4 address it wraps.
    """
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return client_ip


def get_rate_limit_key(request: Request) -> str:
    """Bucket key: the client IP, with IPv6 aggregated to its /64."""
    return _bucket_key(get_rate_limit_ip(request))


class SlidingWindow:
    """Per-key request counter over a sliding window on the monotonic axis.

    Rejected requests are not recorded, so a caller that backs off is admitted
    again once its oldest admitted request leaves the window.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._max_keys = max_keys
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        # Shared by the event loop and threadpool-run dependencies.
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Count one request; ``None`` when admitted, else seconds until a slot frees."""
        with self._lock:
            now = self._clock()
            stamps = self._hits.get(key)
            if stamps is None:
                if len(self._hits) >= self._max_keys:
                    self._make_room(now)
                stamps = self._hits[key] = deque()
            else:
                self._hits.move_to_end(key)
            while stamps and stamps[0] <= now - self._window:
                stamps.popleft()
            if len(stamps) >= self._limit:
                return max(0.0, stamps[0] + self._window - now)
            stamps.append(now)
            return None

    def _make_room(self, now: float) -> None:
        for key in [k for k, stamps in self._hits.items() if not stamps or stamps[-1] <= now - self._window]:
            del self._hits[key]
        # Still full: drop the least recently used caller. Rejecting instead would let
        # an attacker holding many prefixes lock every new legitimate client out.
        while len(self._hits) >= self._max_keys:
            self._hits.popitem(last=False)

    def reset(self) -> None:
        """Forget every counter."""
        with self._lock:
            self._hits.clear()


_windows: weakref.WeakSet[SlidingWindow] = weakref.WeakSet()


def parse_rate_spec(spec: str) -> tuple[int, float]:
    """Parse ``"N/second|minute|hour"`` into ``(limit, window_seconds)``."""
    match = _SPEC_RE.fullmatch(spec.strip())
    if match is None or int(match.group(1)) < 1:
        raise ValueError(f"Invalid rate limit specification: {spec!r}")
    return int(match.group(1)), _UNIT_SECONDS[match.group(2)]


def rate_limit(spec: str, *, clock: Callable[[], float] = time.monotonic) -> Callable[[Request], None]:
    """Build a FastAPI dependency enforcing ``spec`` per client key.

    Every call creates its own budget, so each route is limited independently;
    a handler registered on several paths shares the one dependency and thus one budget.
    """
    limit, window_seconds = parse_rate_spec(spec)
    window = SlidingWindow(limit, window_seconds, clock)
    _windows.add(window)

    def enforce_rate_limit(request: Request) -> None:
        try:
            retry_after = window.hit(get_rate_limit_key(request))
        except Exception:
            # Fail closed: an internal limiter fault must not admit traffic.
            logger.exception("Rate limiter failure; rejecting request")
            retry_after = 1.0
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail=_RATE_LIMIT_DETAIL,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    enforce_rate_limit.rate_limit_spec = spec  # type: ignore[attr-defined]  # read by the route-coverage test
    return enforce_rate_limit


def reset_rate_limits() -> None:
    """Clear every route's counters (tests; counters are otherwise process-lifetime)."""
    for window in list(_windows):
        window.reset()
