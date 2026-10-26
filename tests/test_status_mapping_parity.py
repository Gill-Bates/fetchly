#!/usr/bin/env python3
#
# tests/test_status_mapping_parity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Guards the job-status contract (DRY audit finding 2).

The authoritative groups live in app/db.py and the display metadata in
app/utils/template_filters.py. status_contract() serializes both into the page
bootstrap data, and the frontend (config.js) reads only that payload - the
other JS modules no longer keep their own status tables. This test asserts
three things:

1. the serialized contract matches the authoritative db.py groups and the
   status_label()/status_class() output for every known status;
2. the worker's progress-bearing set (which cannot import a new db constant)
   still agrees with db.PROGRESS_STATUSES;
3. the JS modules consume the single source instead of re-declaring it, so the
   consolidation cannot silently regress.
"""

import re
import unittest
from pathlib import Path

from app import db
from app.utils import template_filters

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATIC_JS = _REPO_ROOT / "app" / "static" / "js"
_TEMPLATES = _REPO_ROOT / "app" / "templates"


class StatusContractSerializationTests(unittest.TestCase):
    """status_contract() mirrors the authoritative db.py groups and labels."""

    def setUp(self) -> None:
        self.contract = template_filters.status_contract()

    def test_groups_match_backend_sets(self) -> None:
        self.assertEqual(set(self.contract["downloadable"]), set(db.DOWNLOADABLE_STATUSES))
        self.assertEqual(set(self.contract["cancellable"]), set(db.CANCELLABLE_JOB_STATUSES))
        self.assertEqual(set(self.contract["retryable"]), set(db.RETRYABLE_JOB_STATUSES))
        self.assertEqual(set(self.contract["terminal"]), set(db.TERMINAL_JOB_STATUSES))
        self.assertEqual(set(self.contract["progress"]), set(db.PROGRESS_STATUSES))

    def test_groups_are_sorted_lists(self) -> None:
        for key in ("downloadable", "cancellable", "retryable", "terminal", "progress"):
            values = self.contract[key]
            self.assertIsInstance(values, list)
            self.assertEqual(values, sorted(values), key)

    def test_meta_covers_every_known_status_with_label_and_color(self) -> None:
        self.assertEqual(set(self.contract["meta"]), set(db._JOB_STATUSES))
        for status in db._JOB_STATUSES:
            entry = self.contract["meta"][status]
            self.assertEqual(entry["label"], template_filters.status_label(status), status)
            self.assertEqual(entry["color"], template_filters.status_class(status), status)

    def test_cancellable_and_terminal_partition_the_lifecycle(self) -> None:
        self.assertFalse(set(self.contract["cancellable"]) & set(self.contract["terminal"]))

    def test_retryable_is_never_cancellable(self) -> None:
        self.assertFalse(set(self.contract["retryable"]) & set(self.contract["cancellable"]))


class WorkerProgressParityTests(unittest.TestCase):
    """The worker owns the progress-bearing writeback and must agree with db.py."""

    def test_worker_progress_set_matches_db(self) -> None:
        from app.workers import worker

        self.assertEqual(set(worker._PROGRESS_BEARING_STATUSES), set(db.PROGRESS_STATUSES))


class StatusSubsetTests(unittest.TestCase):
    """The worker-owned / cancellable / retryable subsets keep their meaning."""

    def test_subsets_are_known_statuses(self) -> None:
        for subset in (
            db.WORKER_OWNED_STATUSES,
            db.CANCELLABLE_JOB_STATUSES,
            db.RETRYABLE_JOB_STATUSES,
            db.PROGRESS_STATUSES,
        ):
            self.assertLessEqual(set(subset), db._JOB_STATUSES)

    def test_subset_membership(self) -> None:
        self.assertEqual(set(db.WORKER_OWNED_STATUSES), {"processing", "downloading", "transcoding"})
        self.assertEqual(
            set(db.CANCELLABLE_JOB_STATUSES),
            {"queued", "processing", "downloading", "transcoding"},
        )
        self.assertEqual(set(db.RETRYABLE_JOB_STATUSES), {"error", "cancelled"})
        self.assertEqual(set(db.PROGRESS_STATUSES), {"downloading", "transcoding"})

    def test_cancellable_jobs_are_never_terminal(self) -> None:
        self.assertFalse(set(db.CANCELLABLE_JOB_STATUSES) & db.TERMINAL_JOB_STATUSES)


class FrontendConsumesSingleSourceTests(unittest.TestCase):
    """The JS modules read the contract instead of re-declaring their own copy."""

    def test_config_js_reads_the_bootstrap_contract(self) -> None:
        source = (_STATIC_JS / "config.js").read_text(encoding="utf-8")
        self.assertIn("dataset.statusContract", source)
        # No hardcoded status set literals remain in config.js.
        self.assertNotRegex(source, r'new Set\(\[\s*"(done|queued|error|analysis)"')

    def test_ui_js_imports_status_meta_instead_of_defining_it(self) -> None:
        source = (_STATIC_JS / "ui.js").read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"import\s*\{[^}]*\bSTATUS_META\b[^}]*\}\s*from\s*\"\./config\.js\"",
        )
        self.assertNotRegex(source, r"\bSTATUS_META\s*=\s*Object\.freeze")

    def test_jobs_js_has_no_private_status_table(self) -> None:
        source = (_STATIC_JS / "jobs.js").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\bconst\s+STATUS\s*=\s*Object\.freeze")
        self.assertNotRegex(source, r"\bSTATUS\.[A-Z]")

    def test_main_js_derives_detail_labels_from_shared_meta(self) -> None:
        source = (_STATIC_JS / "main.js").read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"import\s*\{[^}]*\bSTATUS_META\b[^}]*\}\s*from\s*\"\./config\.js\"",
        )
        self.assertNotRegex(source, r"\bDETAIL_STATUS_LABELS\s*=\s*Object\.freeze")

    def test_progress_set_has_one_js_definition(self) -> None:
        for name in ("ui.js", "jobs.js", "events.js"):
            with self.subTest(module=name):
                source = (_STATIC_JS / name).read_text(encoding="utf-8")
                self.assertRegex(
                    source,
                    r"import\s*\{[^}]*\bPROGRESS_STATUSES\b[^}]*\}\s*from\s*\"\./config\.js\"",
                )
                self.assertNotRegex(source, r"\bPROGRESS_STATUSES\s*=\s*new Set")


class TemplateDownloadableStatusTests(unittest.TestCase):
    """job.html still gates its download button on the downloadable set."""

    def test_job_detail_template_matches_backend_downloadable_statuses(self) -> None:
        job_html_source = (_TEMPLATES / "job.html").read_text(encoding="utf-8")
        match = re.search(r"\(\s*'done',\s*'analysis',\s*'analysis_done'\s*\)", job_html_source)
        assert match, "Expected job.html to gate the download button on the downloadable tuple"
        found = set(re.findall(r"'([^']+)'", match.group(0)))
        self.assertEqual(found, set(db.DOWNLOADABLE_STATUSES))


if __name__ == "__main__":
    unittest.main()
