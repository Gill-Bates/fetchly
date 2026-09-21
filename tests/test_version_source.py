#!/usr/bin/env python3
#
# tests/test_version_source.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""pyproject.toml is the single source of truth for the release version.

A regression that reintroduced a second version file, or fell back to "dev",
would ship mislabelled images.
"""

import tomllib
import unittest
from pathlib import Path

from app.utils import version as version_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


class VersionSourceTests(unittest.TestCase):
    def test_get_version_matches_pyproject(self) -> None:
        version_module.get_version.cache_clear()
        self.assertEqual(version_module.get_version(), _pyproject()["project"]["version"])

    def test_version_is_release_shaped(self) -> None:
        # Same grammar the release workflow enforces before it moves 'latest'.
        version = _pyproject()["project"]["version"]
        self.assertRegex(version, r"^\d+\.\d+(\.\d+)?(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$")

    def test_the_replaced_files_are_gone(self) -> None:
        # constraints.txt is generated per release by the Docker workflow and
        # handed to both architecture builds; a committed copy would pin the
        # images to a stale resolution.
        for stale in ("VERSION", "requirements.txt", "docs/requirements-docs.txt", "constraints.txt"):
            with self.subTest(stale=stale):
                self.assertFalse(
                    (PROJECT_ROOT / stale).exists(),
                    f"{stale} is back; pyproject.toml must stay the only manifest",
                )

    def test_docs_extra_carries_the_mkdocs_toolchain(self) -> None:
        extras = _pyproject()["project"]["optional-dependencies"]
        names = {req.split("==")[0].split(">=")[0].strip() for req in extras["docs"]}
        self.assertIn("mkdocs", names)
        self.assertIn("mkdocs-material", names)

    def test_dev_extra_carries_the_lint_and_test_toolchain(self) -> None:
        extras = _pyproject()["project"]["optional-dependencies"]
        names = {req.split("==")[0].split(">=")[0].strip() for req in extras["dev"]}
        self.assertIn("pytest", names)
        self.assertIn("ruff", names)

    def test_runtime_dependencies_carry_no_version_pin(self) -> None:
        # A release installs the newest resolvable set. The two architectures
        # still agree because the release workflow resolves that set once and
        # constrains both builds to it - a pin here would be a second, stale
        # opinion the images never follow. Lower bounds stay allowed: they say
        # what the code needs, not which version a release ships.
        for requirement in _pyproject()["project"]["dependencies"]:
            with self.subTest(dependency=requirement):
                self.assertNotIn("==", requirement)

    def test_torch_packages_are_bound_to_the_cpu_index(self) -> None:
        # The CPU wheel index is not a torch-only host: it also serves jinja2,
        # numpy, sympy, filelock, fsspec and setuptools. As a plain extra index
        # it would be searched for every name in a resolution and could pin a
        # version PyPI does not carry, while the image installs those from PyPI.
        # `explicit` plus a source binding is what keeps it to the two packages
        # docker/Dockerfile installs from it.
        uv = _pyproject()["tool"]["uv"]
        self.assertNotIn(
            "extra-index-url",
            uv,
            "an extra index is searched for every package; declare an explicit index instead",
        )
        cpu_indexes = [
            index
            for index in uv["index"]
            if index["url"] == "https://download.pytorch.org/whl/cpu"
        ]
        self.assertEqual(len(cpu_indexes), 1)
        self.assertTrue(cpu_indexes[0]["explicit"])
        for package in ("torch", "torchaudio"):
            with self.subTest(package=package):
                self.assertEqual(
                    [{"index": cpu_indexes[0]["name"]}],
                    uv["sources"][package],
                )

    def test_torch_packages_are_direct_dependencies(self) -> None:
        # uv applies a source binding only to a dependency the manifest declares
        # itself. torchaudio arrives through beat-this, so without this line it
        # would be resolved from PyPI - against the CUDA torch - while the image
        # installs the +cpu build from the CPU index.
        names = {
            req.split("==")[0].split(">=")[0].split("[")[0].strip()
            for req in _pyproject()["project"]["dependencies"]
        }
        self.assertIn("torch", names)
        self.assertIn("torchaudio", names)

    def test_runtime_dependencies_cover_the_web_stack(self) -> None:
        names = {
            req.split("==")[0].split(">=")[0].split("[")[0].strip()
            for req in _pyproject()["project"]["dependencies"]
        }
        for required in ("fastapi", "starlette", "uvicorn", "gunicorn", "jinja2"):
            with self.subTest(dependency=required):
                self.assertIn(required, names)


if __name__ == "__main__":
    unittest.main()
