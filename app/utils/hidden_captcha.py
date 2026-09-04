#!/usr/bin/env python3
#
# app/utils/hidden_captcha.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Hidden_Captcha: an invisible, no-interaction anti-bot check for the public
``POST /login`` endpoint. Two signals:

1. **Honeypot field** -- a CSS-hidden form field. A human never fills it; many
   form-fillers populate every input.

2. **Signed time-trap token** -- a token minted by :func:`issue_captcha_token`,
   signed with ``FETCHLY_SECRET_KEY`` (same shape as the session cookie). On
   submit it must be present, correctly signed, not older than
   ``max_age_seconds``, and not *younger* than ``min_age_seconds`` (an
   implausibly fast submit is scripted). Tokens are not single-use.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from enum import StrEnum
from hashlib import sha256
from time import time

# Salt namespacing this token apart from other HMAC uses of the same key, so a
# value minted for one cannot be replayed as the other.
_SALT = "fetchly-hidden-captcha"

# `name` of the CSS-hidden honeypot input; looks tempting to a form-filler.
HONEYPOT_FIELD_NAME = "website"

_TOKEN_VERSION = "v1"  # noqa: S105  # format marker, not a credential

DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60  # form rejected as stale after this
DEFAULT_MIN_AGE_SECONDS = 1.0  # faster submits are scripted; 0 disables


class CaptchaOutcome(StrEnum):
    """Result of :func:`verify_captcha_token`. Only ``OK`` proceeds; the caller
    collapses every other variant into one generic rejection.
    """

    OK = "ok"
    HONEYPOT_FILLED = "honeypot_filled"
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"
    TOO_FAST = "too_fast"


def _sign(secret_key: str, payload: str) -> str:
    return hmac.new(
        f"{secret_key}:{_SALT}".encode(), payload.encode("utf-8"), sha256
    ).hexdigest()


def issue_captcha_token(secret_key: str) -> str:
    """Mint a signed, timestamped token for the login form."""
    payload = f"{_TOKEN_VERSION}:{int(time())}"
    signature = _sign(secret_key, payload)
    raw = f"{payload}:{signature}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_captcha_token(
    *,
    token: str | None,
    honeypot: str | None,
    secret_key: str,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> CaptchaOutcome:
    """Evaluate the anti-bot signals for a login submission.

    ``OK`` only when the honeypot is empty and the token is present, signed,
    unexpired and (if ``min_age_seconds > 0``) not implausibly fresh. ``now``
    is injectable for tests.
    """
    if honeypot is not None and honeypot.strip():
        return CaptchaOutcome.HONEYPOT_FILLED

    if not token:
        return CaptchaOutcome.MISSING

    # Decode + signature check.
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload, signature = raw.rsplit(":", 1)
        version, issued_at_raw = payload.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return CaptchaOutcome.INVALID

    if version != _TOKEN_VERSION:
        return CaptchaOutcome.INVALID

    if not hmac.compare_digest(signature, _sign(secret_key, payload)):
        return CaptchaOutcome.INVALID

    try:
        issued_at = int(issued_at_raw)
    except ValueError:
        return CaptchaOutcome.INVALID

    # Age window.
    current = now if now is not None else time()
    age_seconds = current - issued_at
    if age_seconds < 0:
        return CaptchaOutcome.INVALID
    if age_seconds > max_age_seconds:
        return CaptchaOutcome.EXPIRED
    if min_age_seconds > 0 and age_seconds < min_age_seconds:
        return CaptchaOutcome.TOO_FAST

    return CaptchaOutcome.OK
