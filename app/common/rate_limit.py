#!/usr/bin/env python3
#
# app/common/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from __future__ import annotations

import ipaddress
import os
from functools import cache
from typing import Final, TypeAlias

from fastapi import Request
from slowapi import Limiter

type TrustedProxySpec = str | ipaddress.IPv4Network | ipaddress.IPv6Network

_DEFAULT_TRUSTED_PROXY_IPS: Final = ",".join((
	"127.0.0.1",
	"::1",
	"10.0.0.0/8",
	"172.16.0.0/12",
	"192.168.0.0/16",
	"fc00::/7",
))


def get_trusted_proxy_hosts() -> str:
	"""Return the trusted proxy allow-list used by both Uvicorn and SlowAPI.

	FORWARDED_ALLOW_IPS follows Uvicorn's existing environment variable, so the
	same deployment knob controls both request scope rewriting and rate-limit IP
	extraction.

	Note: This value is cached on first use for the lifetime of the process.
	Restart the app to pick up environment changes.
	"""
	value = str(os.environ.get("FORWARDED_ALLOW_IPS", _DEFAULT_TRUSTED_PROXY_IPS)).strip()
	return value or _DEFAULT_TRUSTED_PROXY_IPS


@cache
def _trusted_proxy_specs() -> tuple[TrustedProxySpec, ...] | None:
	specs: list[TrustedProxySpec] = []
	for raw_item in get_trusted_proxy_hosts().split(","):
		item = raw_item.strip()
		if not item:
			continue
		if item == "*":
			return None
		try:
			specs.append(ipaddress.ip_network(item, strict=False))
		except ValueError:
			specs.append(item.lower())
	return tuple(specs)


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


def _is_trusted_proxy(host: str | None) -> bool:
	normalized = _normalize_ip(host)
	specs = _trusted_proxy_specs()
	if specs is None:
		return True
	if normalized is None:
		return bool(host and host.lower() in specs)

	address = ipaddress.ip_address(normalized)
	for spec in specs:
		if isinstance(spec, str):
			if host and host.lower() == spec:
				return True
			continue
		if address in spec:
			return True
	return False


def _forwarded_client_ip(request: Request) -> str | None:
	client_host = request.client.host if request.client else None
	if not _is_trusted_proxy(client_host):
		return None

	forwarded_for = request.headers.get("x-forwarded-for", "")
	if forwarded_for:
		parts = [_normalize_ip(part) for part in forwarded_for.split(",")]
		normalized_parts = [part for part in parts if part]
		for ip in reversed(normalized_parts):
			if not _is_trusted_proxy(ip):
				return ip
		if normalized_parts:
			return normalized_parts[0]

	return _normalize_ip(request.headers.get("x-real-ip"))


def get_rate_limit_ip(request: Request) -> str:
	"""Resolve a stable client IP for SlowAPI when running behind trusted proxies."""
	forwarded = _forwarded_client_ip(request)
	if forwarded:
		return forwarded
	if request.client and request.client.host:
		return request.client.host
	return "unknown"


limiter = Limiter(key_func=get_rate_limit_ip)