#!/usr/bin/env python3
#
# tests/test_host_stats.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FETCHLY_SECRET_KEY", "test-host-stats-secret")

from app.utils import host_stats


class FormatUptimeTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        self.assertEqual(host_stats._format_uptime(0), "<1m")
        self.assertEqual(host_stats._format_uptime(59), "<1m")
        self.assertEqual(host_stats._format_uptime(60), "1m")
        self.assertEqual(host_stats._format_uptime(3_599), "59m")
        self.assertEqual(host_stats._format_uptime(3_600), "1h 0m")
        self.assertEqual(host_stats._format_uptime(3_661), "1h 1m")
        self.assertEqual(host_stats._format_uptime(90_061), "1d 1h")


class CpuPercentBetweenTests(unittest.TestCase):
    def test_half_busy(self) -> None:
        # idle advanced 50, total advanced 100 -> 50% busy.
        self.assertEqual(host_stats._cpu_percent_between((100, 200), (150, 300)), 50.0)

    def test_fully_idle_and_fully_busy(self) -> None:
        self.assertEqual(host_stats._cpu_percent_between((0, 0), (100, 100)), 0.0)
        self.assertEqual(host_stats._cpu_percent_between((0, 0), (0, 100)), 100.0)

    def test_non_positive_total_delta_returns_none(self) -> None:
        self.assertIsNone(host_stats._cpu_percent_between((10, 20), (10, 20)))
        self.assertIsNone(host_stats._cpu_percent_between((10, 20), (10, 10)))

    def test_result_is_clamped(self) -> None:
        # Idle going backwards would push the raw ratio above 100%.
        self.assertEqual(host_stats._cpu_percent_between((100, 100), (0, 150)), 100.0)


class MemoryUsageTests(unittest.TestCase):
    def test_uses_mem_available_when_present(self) -> None:
        fake = {"MemTotal": 1000, "MemAvailable": 250, "MemFree": 10}
        with patch.object(host_stats, "_read_meminfo", return_value=fake):
            result = host_stats._memory_usage()
        assert result is not None
        self.assertEqual(result["total"], 1000)
        self.assertEqual(result["available"], 250)
        self.assertEqual(result["used"], 750)
        self.assertEqual(result["percent"], 75.0)

    def test_falls_back_to_free_plus_caches(self) -> None:
        fake = {"MemTotal": 1000, "MemFree": 100, "Buffers": 50, "Cached": 150}
        with patch.object(host_stats, "_read_meminfo", return_value=fake):
            result = host_stats._memory_usage()
        assert result is not None
        self.assertEqual(result["available"], 300)
        self.assertEqual(result["used"], 700)

    def test_no_total_returns_none(self) -> None:
        with patch.object(host_stats, "_read_meminfo", return_value={}):
            self.assertIsNone(host_stats._memory_usage())


class StorageUsageTests(unittest.TestCase):
    def test_walks_up_to_existing_parent(self) -> None:
        missing = Path("/opt/fetchly") / "definitely-not-here" / "nested"
        result = host_stats._storage_usage(missing)
        assert result is not None
        self.assertGreater(result["total"], 0)
        self.assertEqual(set(result), {"total", "used", "free", "percent"})


class GetHostStatsTests(unittest.TestCase):
    def test_snapshot_has_all_sections(self) -> None:
        snapshot = asyncio.run(host_stats.get_host_stats(Path("/opt/fetchly")))
        self.assertEqual(set(snapshot), {"storage", "cpu", "memory", "uptime"})

    def test_cpu_section_is_none_when_proc_stat_unavailable(self) -> None:
        with patch.object(host_stats, "_read_cpu_jiffies", return_value=None):
            snapshot = asyncio.run(host_stats.get_host_stats(Path("/opt/fetchly")))
        self.assertIsNone(snapshot["cpu"])

    def test_warm_cpu_path_uses_stored_sample(self) -> None:
        from time import monotonic

        # A sample ~5s old, then a reading 30 jiffies busier out of 100.
        with patch.object(host_stats, "_last_cpu_sample", (monotonic() - 5.0, 1_000, 2_000)), \
             patch.object(host_stats, "_read_cpu_jiffies", return_value=(1_070, 2_100)):
            percent = asyncio.run(host_stats._sample_cpu_percent())
        self.assertAlmostEqual(percent, 30.0, places=5)


if __name__ == "__main__":
    unittest.main()
