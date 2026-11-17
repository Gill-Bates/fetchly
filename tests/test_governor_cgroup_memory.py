"""Cgroup memory headroom must not count reclaimable inactive file cache."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.runtime import governor as gov
from app.runtime.governor import Governor

MIB = 1024 * 1024
_ORIG_READ_TEXT = Path.read_text


class CgroupHeadroomTests(unittest.TestCase):
    def _read(self, files: dict[str, str]) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            for rel, content in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            with (
                patch.object(gov, "_CGROUP_ROOT", tmp),
                patch("app.runtime.governor.Path.read_text", autospec=True, side_effect=self._guard),
            ):
                return Governor()._read_memory_available_mb_uncached()

    @staticmethod
    def _guard(path: Path, *args: object, **kwargs: object) -> str:
        # Block the /proc/meminfo fallback so it never reads the real host.
        if str(path).startswith("/proc"):
            raise OSError("blocked")
        return _ORIG_READ_TEXT(path, *args, **kwargs)

    def _v2(self, current: int, stat: str | None, maximum: str = str(512 * MIB)) -> int:
        files = {"memory.current": str(current), "memory.max": maximum}
        if stat is not None:
            files["memory.stat"] = stat
        return self._read(files)

    def test_inactive_file_is_reclaimable(self) -> None:
        stat = f"anon {100 * MIB}\ninactive_file {250 * MIB}\nactive_file {50 * MIB}\n"
        # 512 - (400 - 250) = 362 (was 112)
        self.assertEqual(self._v2(400 * MIB, stat), 362)

    def test_active_file_not_counted(self) -> None:
        stat = f"inactive_file 0\nactive_file {300 * MIB}\n"
        self.assertEqual(self._v2(400 * MIB, stat), 112)

    def test_inactive_larger_than_usage_is_clamped(self) -> None:
        self.assertEqual(self._v2(100 * MIB, f"inactive_file {999 * MIB}\n"), 512)

    def test_missing_stat_keeps_old_behaviour(self) -> None:
        self.assertEqual(self._v2(400 * MIB, None), 112)

    def test_garbage_stat_keeps_old_behaviour(self) -> None:
        self.assertEqual(self._v2(400 * MIB, "inactive_file nope\n"), 112)

    def test_usage_over_limit_is_zero(self) -> None:
        self.assertEqual(self._v2(600 * MIB, "inactive_file 0\n"), 0)

    def test_max_unlimited_skips_v2(self) -> None:
        self.assertEqual(self._v2(400 * MIB, "inactive_file 1\n", maximum="max"), -1)

    def test_v1_uses_total_inactive_file(self) -> None:
        files = {
            "memory/memory.usage_in_bytes": str(400 * MIB),
            "memory/memory.limit_in_bytes": str(512 * MIB),
            "memory/memory.stat": f"total_inactive_file {250 * MIB}\n",
        }
        self.assertEqual(self._read(files), 362)

    def test_v1_without_stat_keeps_old_behaviour(self) -> None:
        files = {
            "memory/memory.usage_in_bytes": str(400 * MIB),
            "memory/memory.limit_in_bytes": str(512 * MIB),
        }
        self.assertEqual(self._read(files), 112)


if __name__ == "__main__":
    unittest.main()
