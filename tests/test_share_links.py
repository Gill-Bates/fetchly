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


class ShareLinkIdempotencyTests(IsolatedDbTestCase):
    """Repeated "Share" clicks on the same job must not mint new URLs or move
    the download counter: create_share_link() only reuses, and only redeem
    (GET /share/{token}) is allowed to advance use_count.
    """

    def _job(self) -> str:
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, "https://example.com/video", "audio", "max", "done")
        return job_id

    def test_repeated_creation_returns_the_same_token(self) -> None:
        job_id = self._job()
        first = db.create_share_link(job_id, 0)
        for _ in range(5):
            self.assertEqual(db.create_share_link(job_id, 0), first)

    def test_creation_never_advances_use_count(self) -> None:
        job_id = self._job()
        for _ in range(5):
            token = db.create_share_link(job_id, 0)

        link = db.get_share_link(token)
        assert link is not None
        self.assertEqual(link["use_count"], 0)

    def test_a_different_max_uses_setting_mints_a_fresh_token(self) -> None:
        # Reuse is scoped to the snapshotted max_uses so a setting change does
        # not silently hand out a link under the old limit.
        job_id = self._job()
        unlimited = db.create_share_link(job_id, 0)
        limited = db.create_share_link(job_id, 3)
        self.assertNotEqual(unlimited, limited)

    def test_an_exhausted_link_is_not_reused(self) -> None:
        job_id = self._job()
        token = db.create_share_link(job_id, 1)
        self.assertTrue(db.consume_share_link(token))

        # The one allowed use is spent: a fresh "Share" click must not hand
        # out the dead token again.
        self.assertNotEqual(db.create_share_link(job_id, 1), token)

    def test_redeeming_is_the_only_thing_that_advances_use_count(self) -> None:
        job_id = self._job()
        token = db.create_share_link(job_id, 0)

        db.consume_share_link(token)
        db.consume_share_link(token)
        # A repeat "Share" click after downloads happened must still return
        # the same token and must not touch use_count itself.
        self.assertEqual(db.create_share_link(job_id, 0), token)

        link = db.get_share_link(token)
        assert link is not None
        self.assertEqual(link["use_count"], 2)


if __name__ == "__main__":
    unittest.main()
