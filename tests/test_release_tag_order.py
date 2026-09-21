#!/usr/bin/env python3
#
# tests/test_release_tag_order.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The release workflow's tag ordering decides whether pruning may run.

.github/workflows/docker-build.yml compares the version being released against
the tags already on Docker Hub. A 'nothing newer is published' answer moves
'latest' and releases the job that deletes every other version tag, so a
mis-ordered comparison is destructive rather than cosmetic. The ordering
function is lifted out of the workflow here and exercised directly.
"""

import re
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "docker-build.yml"


def _load_key():
    """Return the workflow's own key() function, executed as the workflow has it."""
    source = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"^(?P<indent>[ ]*)ARCH_SUFFIXES = \{.*?\n(?P=indent)current = os\.environ",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "the tag-ordering block is no longer recognisable in docker-build.yml"
    block = textwrap.dedent(source[match.start() : match.end() - len("current = os.environ")])
    namespace: dict = {"re": re}
    exec(compile(block, str(WORKFLOW), "exec"), namespace)  # noqa: S102
    return namespace["key"]


class ReleaseTagOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key = staticmethod(_load_key())

    def test_architecture_fragments_are_not_versions(self) -> None:
        # Build artefacts of the workflow itself; comparing them as prereleases
        # of their own version would make every release look outdated.
        for tag in ("1.6-amd64", "1.6-arm64", "latest", "sha-abc123"):
            with self.subTest(tag=tag):
                self.assertIsNone(self.key(tag))

    def test_prerelease_sorts_below_its_release(self) -> None:
        self.assertLess(self.key("1.6-rc.1"), self.key("1.6"))

    def test_numeric_prerelease_parts_compare_as_numbers(self) -> None:
        # Lexicographic suffix comparison puts rc.10 below rc.2 and would let a
        # re-run of rc.2 prune the newer rc.10.
        self.assertLess(self.key("1.6-rc.2"), self.key("1.6-rc.10"))
        self.assertLess(self.key("1.6-rc.9"), self.key("1.6-rc.11"))

    def test_named_prerelease_parts_stay_comparable(self) -> None:
        self.assertLess(self.key("1.6-alpha.1"), self.key("1.6-beta.1"))
        self.assertLess(self.key("1.6-rc.1"), self.key("1.6-rc.1.1"))

    def test_two_and_three_component_versions_are_one_version(self) -> None:
        # fetchly tags both shapes (v1.4, v1.3.0); treating 1.6 as older than
        # 1.6.0 would let one prune the other.
        self.assertEqual(self.key("1.6"), self.key("1.6.0"))
        self.assertLess(self.key("1.6"), self.key("1.6.1"))

    def test_release_components_compare_numerically(self) -> None:
        self.assertLess(self.key("1.9"), self.key("1.10"))
        self.assertLess(self.key("1.10"), self.key("2.0"))

    def test_longer_versions_are_not_truncated_into_equality(self) -> None:
        self.assertLess(self.key("1.6.0"), self.key("1.6.0.1"))


if __name__ == "__main__":
    unittest.main()
