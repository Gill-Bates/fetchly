#!/usr/bin/env python3
#
# app/bpm_cluster.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from collections import Counter
from collections.abc import Iterable

__all__ = ["cluster_bpms"]


def cluster_bpms(bpms: Iterable[int]) -> list[tuple[int, int]]:
    """Group positive BPM values into 5-BPM buckets.

    Values that are zero or negative are silently ignored.

    Returns:
        A list of ``(bucket_bpm, count)`` tuples sorted by count descending,
        then by bucket ascending.
    """
    buckets: Counter[int] = Counter()
    for bpm in bpms:
        if bpm <= 0:
            continue
        buckets[round(bpm / 5) * 5] += 1

    return sorted(buckets.items(), key=lambda item: (-item[1], item[0]))