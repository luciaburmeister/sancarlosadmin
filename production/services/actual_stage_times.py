"""Aggregate real shop-floor session times per stage for a product.

Feeds the ProductTemplate planning page with measured averages so standard
times can be calibrated from actual production data.
"""

import statistics
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple

from production.models import ProductionItemSession
from production.services.intervals import merged_total_seconds

# Below this many orders there is no statistical basis to call a value an
# outlier, so every measurement is kept.
MIN_ORDERS_FOR_OUTLIER_FILTER = 4
IQR_FENCE_MULTIPLIER = 1.5


class StageRun(NamedTuple):
    """One order's merged wall-clock run through a stage."""

    minutes: float
    worker_count: int


@dataclass(frozen=True)
class StageActuals:
    """Measured statistics for one stage of a product, across orders."""

    avg_minutes: float
    min_minutes: float
    max_minutes: float
    order_count: int
    excluded_count: int
    avg_employees: float | None


def normalize_stage(stage: str) -> str:
    return unicodedata.normalize("NFC", stage or "")


def filter_outliers(runs: list[StageRun]) -> tuple[list[StageRun], int]:
    """Drop runs whose duration falls outside Tukey's fences (1.5 x IQR).

    Returns (kept_runs, excluded_count).
    """
    if len(runs) < MIN_ORDERS_FOR_OUTLIER_FILTER:
        return runs, 0

    durations = [run.minutes for run in runs]
    q1, _, q3 = statistics.quantiles(durations, n=4, method="inclusive")
    iqr = q3 - q1
    low = q1 - IQR_FENCE_MULTIPLIER * iqr
    high = q3 + IQR_FENCE_MULTIPLIER * iqr

    kept = [run for run in runs if low <= run.minutes <= high]
    return kept, len(runs) - len(kept)


def summarize_stage_runs(runs: list[StageRun]) -> StageActuals:
    """Roll a stage's per-order runs up into stats, excluding outliers."""
    kept, excluded_count = filter_outliers(runs)
    minutes = [run.minutes for run in kept]
    worker_counts = [run.worker_count for run in kept if run.worker_count]
    return StageActuals(
        avg_minutes=round(sum(minutes) / len(minutes), 1),
        min_minutes=round(min(minutes), 1),
        max_minutes=round(max(minutes), 1),
        order_count=len(minutes),
        excluded_count=excluded_count,
        avg_employees=(
            round(sum(worker_counts) / len(worker_counts), 1)
            if worker_counts
            else None
        ),
    )


def collect_stage_runs(sessions) -> dict[str, list[StageRun]]:
    """Group completed sessions into per-order StageRuns, keyed by stage.

    Sessions for the same (ticket, stage) count as one run: overlapping
    intervals are merged so concurrent workers are not double-counted.
    """
    intervals = defaultdict(list)
    workers = defaultdict(set)
    rows = sessions.values_list(
        "production_item__ticket_id",
        "production_stage",
        "start_time",
        "end_time",
        "created_by_id",
    )
    for ticket_id, stage, start, end, created_by_id in rows:
        key = (ticket_id, normalize_stage(stage))
        intervals[key].append((start, end))
        if created_by_id:
            workers[key].add(created_by_id)

    runs = defaultdict(list)
    for (ticket_id, stage), spans in intervals.items():
        runs[stage].append(
            StageRun(
                minutes=merged_total_seconds(spans) / 60,
                worker_count=len(workers.get((ticket_id, stage), ())),
            )
        )
    return runs


def compute_actual_stage_times(product) -> dict[str, StageActuals]:
    """Measured per-stage stats for a product, keyed by NFC stage name."""
    sessions = ProductionItemSession.objects.completed().for_product(product)
    return {
        stage: summarize_stage_runs(runs)
        for stage, runs in collect_stage_runs(sessions).items()
    }
