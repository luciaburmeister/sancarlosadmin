from datetime import datetime, timedelta

from django.test import SimpleTestCase

from production.services.actual_stage_times import StageRun, filter_outliers
from production.services.intervals import merge_intervals, merged_total_seconds


def _t(minutes: int) -> datetime:
    return datetime(2026, 7, 1, 10, 0) + timedelta(minutes=minutes)


class MergeIntervalsTest(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(merge_intervals([]), [])

    def test_disjoint_intervals_are_kept(self):
        intervals = [(_t(0), _t(10)), (_t(20), _t(30))]
        self.assertEqual(merge_intervals(intervals), intervals)

    def test_overlapping_and_contained_intervals_are_merged(self):
        intervals = [
            (_t(30), _t(90)),
            (_t(0), _t(60)),
            (_t(40), _t(50)),  # fully contained
        ]
        self.assertEqual(merge_intervals(intervals), [(_t(0), _t(90))])
        self.assertEqual(merged_total_seconds(intervals), 90 * 60)

    def test_adjacent_intervals_are_merged(self):
        intervals = [(_t(0), _t(30)), (_t(30), _t(45))]
        self.assertEqual(merge_intervals(intervals), [(_t(0), _t(45))])


class FilterOutliersTest(SimpleTestCase):
    def test_small_samples_are_untouched(self):
        runs = [StageRun(30, 1), StageRun(32, 1), StageRun(600, 1)]
        kept, excluded = filter_outliers(runs)
        self.assertEqual(kept, runs)
        self.assertEqual(excluded, 0)

    def test_high_outlier_is_excluded(self):
        runs = [
            StageRun(28, 1),
            StageRun(30, 1),
            StageRun(32, 1),
            StageRun(600, 2),
        ]
        kept, excluded = filter_outliers(runs)
        self.assertEqual(excluded, 1)
        self.assertNotIn(StageRun(600, 2), kept)

    def test_uniform_data_keeps_everything(self):
        runs = [StageRun(30, 1)] * 6
        kept, excluded = filter_outliers(runs)
        self.assertEqual(len(kept), 6)
        self.assertEqual(excluded, 0)
