#!/usr/bin/env python3
#
# tests/test_analysis_worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Analysis worker contracts: result caching, naming, and shutdown response."""

import threading
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from app import analysis_worker
from app.audio_analysis import AudioAnalysisResult
from app.db import get_audio_analysis_cache, get_job, insert_job
from tests._support import IsolatedDbTestCase

JOB = ("22222222-2222-2222-2222-222222222222", "https://example.com/a", "audio", "max")


class _FakeProcess:
    """Minimal stand-in for a multiprocessing child (see _await_process)."""

    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive
        self.pid = 4242
        self.terminated = False
        self.killed = False

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        _ = timeout

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False


class AwaitProcessTests(unittest.TestCase):
    """The parent must react to a shutdown instead of waiting out the timeout."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(analysis_worker._shutdown_event.clear)

    def test_a_shutdown_ends_the_child_and_aborts_the_wait(self) -> None:
        process = _FakeProcess()
        analysis_worker._shutdown_event.set()

        # A one-hour timeout must not delay this: the shutdown check comes first.
        with self.assertRaises(analysis_worker.AnalysisInterruptedError):
            analysis_worker._await_process(process, 3600.0)

        self.assertTrue(process.terminated)

    def test_an_overrun_child_is_ended_and_reported_as_a_timeout(self) -> None:
        process = _FakeProcess()

        with self.assertRaises(TimeoutError):
            analysis_worker._await_process(process, 0.0)

        self.assertTrue(process.terminated)

    def test_a_finished_child_returns_without_being_signalled(self) -> None:
        process = _FakeProcess(alive=False)

        analysis_worker._await_process(process, 3600.0)

        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)


class StopAnalysisWorkersTests(unittest.TestCase):
    def test_registered_children_are_terminated(self) -> None:
        process = _FakeProcess()
        with analysis_worker._active_analysis_processes_lock:
            analysis_worker._active_analysis_processes.add(process)  # type: ignore[arg-type]
        self.addCleanup(analysis_worker._active_analysis_processes.discard, process)  # type: ignore[arg-type]
        self.addCleanup(analysis_worker._shutdown_event.clear)

        analysis_worker.stop_analysis_workers(timeout=0.1)

        self.assertTrue(process.terminated)


class ApplyAnalysisTests(IsolatedDbTestCase):
    """What a completed analysis writes to the cache, the row, and the disk."""

    def setUp(self) -> None:
        super().setUp()
        self.job_id = JOB[0]
        insert_job(*JOB, "analysis")
        self.audio = Path(self._tmp.name) / "Some Track.source.opus"
        self.audio.write_bytes(b"not really audio")
        self.job = analysis_worker.AnalysisJob(job_id=self.job_id, file_path=self.audio)
        patcher = patch.object(analysis_worker, "_status_callback", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, result: AudioAnalysisResult) -> None:
        with (
            patch.object(analysis_worker, "_extract_analysis_with_timeout", return_value=result),
            # The governor is configured at application startup, not in a unit test.
            patch.object(
                type(analysis_worker.governor),
                "analysis_semaphore_sync",
                new_callable=PropertyMock,
                return_value=threading.Semaphore(1),
            ),
        ):
            analysis_worker._apply_analysis(self.job)

    def _audio_hash(self) -> str:
        job = get_job(self.job_id)
        assert job is not None
        return str(job["audio_hash"])

    def test_a_detected_bpm_is_cached(self) -> None:
        self._run(AudioAnalysisResult(bpm=94, confidence=0.8))

        row = get_audio_analysis_cache(self._audio_hash())
        assert row is not None
        self.assertEqual(row["bpm"], 94)

    def test_a_run_without_a_usable_bpm_is_cached_too(self) -> None:
        # Otherwise every later download of the same audio repeats the full
        # detector cascade just to reach the same "no BPM" answer.
        self._run(AudioAnalysisResult(bpm=None, confidence=0.1))

        row = get_audio_analysis_cache(self._audio_hash())
        assert row is not None
        self.assertIsNone(row["bpm"])

    def test_the_stored_file_keeps_its_name(self) -> None:
        # app/bpm_naming.py owns the BPM tag, and it only ever decorates the
        # download name: the ".source" marker the media routes key off has to
        # stay at the end of the stem on disk.
        self._run(AudioAnalysisResult(bpm=94, confidence=0.8))

        self.assertTrue(self.audio.is_file())
        self.assertEqual(self.audio.stem, "Some Track.source")

    def test_the_job_reaches_analysis_done(self) -> None:
        self._run(AudioAnalysisResult(bpm=94, confidence=0.8))

        job = get_job(self.job_id)
        assert job is not None
        self.assertEqual(job["status"], "analysis_done")
        self.assertEqual(job["bpm"], 94)
        self.assertIsNotNone(job["finished_at"])
