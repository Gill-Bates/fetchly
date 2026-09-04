#!/usr/bin/env python3
#
# tests/test_status_mapping_parity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Guards the job-status knowledge that is independently mirrored across
app/db.py, app/utils/template_filters.py, the Jinja templates, and several
static/js files (see DRY audit finding 3). These sets/labels have no single
generated source of truth, so this test is the safety net that catches drift
between them until they are consolidated behind one server-exported payload.
"""

import re
import unittest
from pathlib import Path

from app import db
from app.utils import template_filters

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATIC_JS = _REPO_ROOT / "app" / "static" / "js"
_TEMPLATES = _REPO_ROOT / "app" / "templates"


def _extract_js_string_set(source: str, const_name: str) -> set[str]:
    """Extract string literals from a `new Set([...])` JS declaration."""
    match = re.search(rf"{const_name}\s*=\s*new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, f"Could not find JS Set declaration for {const_name}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _extract_js_object_keys(source: str, const_name: str) -> set[str]:
    """Extract top-level keys from an `export const NAME = Object.freeze({...})` block."""
    match = re.search(rf"{const_name}\s*=\s*Object\.freeze\(\{{(.*?)\}}\)", source, re.DOTALL)
    assert match, f"Could not find JS object declaration for {const_name}"
    body = match.group(1)
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.MULTILINE))


def _extract_js_label(source: str, const_name: str, key: str) -> str | None:
    match = re.search(rf"{const_name}\s*=\s*Object\.freeze\(\{{(.*?)\}}\)", source, re.DOTALL)
    assert match, f"Could not find JS object declaration for {const_name}"
    body = match.group(1)
    key_match = re.search(rf'\b{key}\s*:\s*\{{[^}}]*label:\s*"([^"]+)"', body)
    if key_match:
        return key_match.group(1)
    key_match = re.search(rf'\b{key}\s*:\s*"([^"]+)"', body)
    return key_match.group(1) if key_match else None


class StatusSetParityTests(unittest.TestCase):
    """DOWNLOADABLE/CANCELLABLE/TERMINAL status sets: db.py vs. config.js vs. templates."""

    def test_config_js_matches_backend_status_sets(self) -> None:
        config_source = (_STATIC_JS / "config.js").read_text(encoding="utf-8")

        self.assertEqual(
            _extract_js_string_set(config_source, "DOWNLOADABLE_STATUSES"),
            set(db.DOWNLOADABLE_STATUSES),
        )
        self.assertEqual(
            _extract_js_string_set(config_source, "TERMINAL_STATUSES"),
            set(db.TERMINAL_JOB_STATUSES),
        )
        # CANCELLABLE_STATUSES (config.js) has no named backend constant; it
        # must match the in-flight statuses guarded server-side in worker.py's
        # transition helpers ("processing", "downloading", "transcoding") plus
        # "queued" (cancellable before a worker ever picks it up).
        self.assertEqual(
            _extract_js_string_set(config_source, "CANCELLABLE_STATUSES"),
            {"queued", "processing", "downloading", "transcoding"},
        )

    def test_action_button_template_matches_backend_status_sets(self) -> None:
        action_btn_source = (_TEMPLATES / "_action_btn.html").read_text(encoding="utf-8")

        downloadable_match = re.search(
            r'\{% if job\["status"\] in \(([^)]+)\) %\}', action_btn_source
        )
        assert downloadable_match
        downloadable = set(re.findall(r'"([^"]+)"', downloadable_match.group(1)))
        self.assertEqual(downloadable, set(db.DOWNLOADABLE_STATUSES))

        cancellable_match = re.search(
            r'\{% elif job\["status"\] in \(([^)]+)\) %\}', action_btn_source
        )
        assert cancellable_match
        cancellable = set(re.findall(r'"([^"]+)"', cancellable_match.group(1)))
        self.assertEqual(cancellable, {"queued", "processing", "downloading", "transcoding"})

    def test_job_detail_template_matches_backend_downloadable_statuses(self) -> None:
        job_html_source = (_TEMPLATES / "job.html").read_text(encoding="utf-8")
        match = re.search(r"\(\s*'done',\s*'analysis',\s*'analysis_done'\s*\)", job_html_source)
        assert match, "Expected job.html to hardcode the downloadable status tuple"
        found = set(re.findall(r"'([^']+)'", match.group(0)))
        self.assertEqual(found, set(db.DOWNLOADABLE_STATUSES))


class StatusLabelParityTests(unittest.TestCase):
    """Status -> display label: template_filters.py vs. ui.js vs. main.js.

    All known statuses must resolve to the *same* label everywhere a job's
    status is shown to the user, closing the "Done" vs. "Completed" drift
    found in the DRY audit.
    """

    def _all_known_statuses(self) -> set[str]:
        return set(db._JOB_STATUSES)

    def test_ui_js_status_labels_match_backend_labels(self) -> None:
        ui_source = (_STATIC_JS / "ui.js").read_text(encoding="utf-8")
        for status in self._all_known_statuses():
            backend_label = template_filters.status_label(status)
            js_label = _extract_js_label(ui_source, "STATUS_META", status)
            self.assertEqual(
                js_label,
                backend_label,
                f"ui.js STATUS_META[{status!r}].label ({js_label!r}) must match "
                f"status_label({status!r}) ({backend_label!r})",
            )

    def test_main_js_detail_labels_match_backend_labels(self) -> None:
        main_source = (_STATIC_JS / "main.js").read_text(encoding="utf-8")
        for status in self._all_known_statuses():
            backend_label = template_filters.status_label(status)
            js_label = _extract_js_label(main_source, "DETAIL_STATUS_LABELS", status)
            # main.js intentionally collapses in-flight statuses to one
            # coarse "Processing" label instead of the finer-grained pill
            # text; only terminal statuses must match exactly.
            if status in db.TERMINAL_JOB_STATUSES:
                self.assertEqual(
                    js_label,
                    backend_label,
                    f"main.js DETAIL_STATUS_LABELS[{status!r}] ({js_label!r}) must match "
                    f"status_label({status!r}) ({backend_label!r}) for a terminal status",
                )


class ProgressStatusParityTests(unittest.TestCase):
    """Which statuses carry a live progress percentage: ui.js vs. jobs.js.

    worker.py only ever emits progress=... for "downloading" and
    "transcoding" (see _DownloadProgress.feed/finish and
    _emit_ffmpeg_progress); both JS-side sets must agree with that and with
    each other.
    """

    def test_ui_js_and_jobs_js_agree_on_progress_bearing_statuses(self) -> None:
        ui_source = (_STATIC_JS / "ui.js").read_text(encoding="utf-8")
        jobs_source = (_STATIC_JS / "jobs.js").read_text(encoding="utf-8")

        ui_progress_match = re.search(r"PROGRESS_STATUSES\s*=\s*new Set\(\[(.*?)\]\)", ui_source)
        assert ui_progress_match
        ui_progress = set(re.findall(r'"([^"]+)"', ui_progress_match.group(1)))

        jobs_progress_match = re.search(
            r"PROGRESS_BEARING_STATUSES\s*=\s*new Set\(\[(.*?)\]\)", jobs_source
        )
        assert jobs_progress_match
        jobs_progress_raw = jobs_progress_match.group(1)
        jobs_progress = set(re.findall(r"STATUS\.([A-Z_]+)", jobs_progress_raw))
        # jobs.js references STATUS.DOWNLOADING/STATUS.TRANSCODING; translate
        # to the same lowercase status strings ui.js uses directly.
        jobs_progress = {name.lower() for name in jobs_progress}

        self.assertEqual(ui_progress, {"downloading", "transcoding"})
        self.assertEqual(jobs_progress, {"downloading", "transcoding"})
        self.assertEqual(ui_progress, jobs_progress)


if __name__ == "__main__":
    unittest.main()
