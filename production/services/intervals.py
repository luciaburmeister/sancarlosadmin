"""Pure helpers for working with (start, end) time intervals.

Used wherever concurrent production sessions must be counted as a single
run of wall-clock time (planning calibration, stage time reports).
"""

from datetime import datetime


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping or adjacent intervals into a minimal disjoint set."""
    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda interval: interval[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def merged_total_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    """Total covered seconds, counting overlapping intervals only once."""
    return sum(
        (end - start).total_seconds() for start, end in merge_intervals(intervals)
    )
