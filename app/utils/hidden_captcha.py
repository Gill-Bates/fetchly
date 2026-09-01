#!/usr/bin/env python3
#
# app/utils/hidden_captcha.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Hidden_Captcha: an invisible, no-user-interaction anti-bot check for the
public, pre-authentication ``POST /login`` endpoint.

Combines the two standard invisible signals, the same technique used by the
sister project (vocalix):

1. **Honeypot field** -- the login page renders a form field that is hidden
   from humans via CSS (``.hp-field``, off-screen) but present in the DOM.
   A real user never sees or fills it; many automated form-fillers blindly
   populate every input, so a non-empty honeypot value is a strong bot tell
   with effectively zero false positives.

2. **Signed time-trap token** -- the login page embeds a token minted by
   :func:`issue_captcha_token`, signed with the app's own session-signing
   key (``FETCHLY_SECRET_KEY``, the same anchor ``app/session.py`` uses for
   the session cookie -- see that module's ``_encode_token`` for the same
   base64url(payload + hmac) shape this reuses). On submit the token must:
   - be present and carry a valid signature (a bot POSTing straight at the
     endpoint without first loading the page has no valid token),
   - not be older than ``max_age_seconds`` (a stale form is rejected; tokens
     remain valid until expiry and are not single-use),
   - not be *younger* than ``min_age_seconds`` (a form submitted
     implausibly fast after being served is almost certainly scripted).

No third-party dependency: fetchly already hand-rolls its own HMAC-based
token signing (see ``app/session.py`` and ``app/routes/auth.py``), so this
follows the same pattern instead of pulling in a package like
``itsdangerous`` for a single small use.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from enum import StrEnum
from hashlib import sha256
from time import time

# Signing salt namespacing this token apart from every other HMAC use of the
# same secret key (e.g. the session cookie in ``app/session.py``), so a
# value minted for one can never be replayed as the other.
_SALT = "fetchly-hidden-captcha"

# The HTML `name` of the honeypot input the login page renders (hidden from
# humans via CSS). Deliberately looks like a legitimate, tempting field so a
# naive form-filling bot populates it; a real user never sees it.
HONEYPOT_FIELD_NAME = "website"

# Token format version, so a future format change can be told apart from a
# tampered value.
_TOKEN_VERSION = "v1"

# A form older than this is rejected as stale. Tokens remain valid until
# expiry and are not single-use.
DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60  # 6 hours

# A form submitted faster than this after being served is treated as
# scripted. Pass 0 to disable the too-fast check entirely.
DEFAULT_MIN_AGE_SECONDS = 1.0


class CaptchaOutcome(StrEnum):
    """Result discriminator for :func:`verify_captcha_token`.

    Only ``OK`` permits the request to proceed. Every other variant means
    "treat as a bot" -- the caller collapses them all into a single,
    generic rejection so an attacker can never learn *which* signal
    tripped.
    """

    OK = "ok"
    HONEYPOT_FILLED = "honeypot_filled"
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"
    TOO_FAST = "too_fast"


def _sign(secret_key: str, payload: str) -> str:
    return hmac.new(
        f"{secret_key}:{_SALT}".encode("utf-8"), payload.encode("utf-8"), sha256
    ).hexdigest()


def issue_captcha_token(secret_key: str) -> str:
    """Mint a fresh, signed, timestamped hidden-captcha token.

    Embedded verbatim into the login form by the login view; returned to
    the server on submit for :func:`verify_captcha_token`.
    """
    payload = f"{_TOKEN_VERSION}:{int(time())}"
    signature = _sign(secret_key, payload)
    raw = f"{payload}:{signature}".encode("utf-8")
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
    """Evaluate the invisible anti-bot signals for a login submission.

    Returns ``CaptchaOutcome.OK`` only when the honeypot is empty AND the
    token is present, correctly signed, not expired, and (when
    ``min_age_seconds > 0``) not implausibly fresh. The honeypot is checked
    first -- it is the cheapest signal and needs no cryptography. ``now`` is
    injectable so tests can exercise the age windows deterministically.
    """
    # 1. Honeypot: any non-empty (non-whitespace) value means a bot filled a
    #    field no human ever sees.
    if honeypot is not None and honeypot.strip():
        return CaptchaOutcome.HONEYPOT_FILLED

    # 2. Token must be present at all.
    if not token:
        return CaptchaOutcome.MISSING

    # 3. Decode + signature check.
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

    # 4. Age window.
    current = now if now is not None else time()
    age_seconds = current - issued_at
    if age_seconds < 0:
        return CaptchaOutcome.INVALID
    if age_seconds > max_age_seconds:
        return CaptchaOutcome.EXPIRED
    if min_age_seconds > 0 and age_seconds < min_age_seconds:
        return CaptchaOutcome.TOO_FAST

    return CaptchaOutcome.OK
