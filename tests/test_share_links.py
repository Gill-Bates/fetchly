#!/usr/bin/env python3
#
# tests/test_share_links.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Share-link tokens: entropy, reuse, and what the redeem route accepts."""

import unittest
import uuid

from app import db
from app.routes.share import _TOKEN_RE
from tests._support import IsolatedDbTestCase

# 22 URL-safe characters over a 64-symbol alphabet = 132 raw bits, of which
# secrets.token_urlsafe(16) contributes 128.
_MIN_TOKEN_LENGTH = 22


class ShareTokenShapeTests(IsolatedDbTestCase):
    def _job(self) -> str:
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/video", "audio", "max", "done")
        return job_id

    def test_token_carries_at_least_128_bits(self) -> None:
        """The token is the only authorization a public download has, so its
        length has to make guessing infeasible without help from a rate limit.
        """
        token = db.create_share_link(self._job(), 0)
        self.assertGreaterEqual(len(token), _MIN_TOKEN_LENGTH)

    def test_token_is_accepted_by_the_redeem_route(self) -> None:
        token = db.create_share_link(self._job(), 0)
        self.assertIsNotNone(_TOKEN_RE.fullmatch(token))

    def test_tokens_are_distinct_per_job(self) -> None:
        self.assertNotEqual(
            db.create_share_link(self._job(), 0),
            db.create_share_link(self._job(), 0),
        )

    def test_shorter_legacy_tokens_still_pass_the_redeem_check(self) -> None:
        # Links handed out by an earlier version must not start 404ing.
        self.assertIsNotNone(_TOKEN_RE.fullmatch("abcd1234"))

    def test_redeem_check_rejects_malformed_tokens(self) -> None:
        for token in ("", "short", "a" * 129, "has spaces", "has/slash", "has.dot"):
            with self.subTest(token=token):
                self.assertIsNone(_TOKEN_RE.fullmatch(token))


if __name__ == "__main__":
    unittest.main()
