from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from production.services.schedule_geometry import (
    MIN_BLOCK_MINUTES,
    ScheduleWindow,
    fmt_pct,
    pack,
    split_overlaps,
)
from production.services.working_calendar import WorkingCalendar
from production.tests.factories import add_schedule, make_company

FRIDAY = datetime(2026, 9, 11)
MONDAY = datetime(2026, 9, 14)


@dataclass
class FakeBlock:
    """Stand-in for a UI block; pack() only touches these attributes."""

    sequence: int
    left_pct: float
    width_pct: float
    is_displaced: bool = False


class ScheduleWindowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)  # Mon-Fri 08:00-14:00 and 15:00-18:00
        cls.tz = timezone.get_current_timezone()
        cls.calendar = WorkingCalendar.for_company(cls.company)

    def at(self, base: datetime, hour: int, minute: int = 0) -> datetime:
        return base.replace(hour=hour, minute=minute, tzinfo=self.tz)

    def one_day(self) -> ScheduleWindow:
        return ScheduleWindow.build(
            self.calendar, self.at(FRIDAY, 0), self.at(FRIDAY, 23, 59)
        )

    # --- segments --------------------------------------------------------

    def test_a_split_shift_becomes_two_segments(self):
        window = self.one_day()

        self.assertEqual(len(window.segments), 2)
        self.assertEqual(window.total_minutes, 10 * 60)
        self.assertAlmostEqual(window.segments[0].width_pct, 60)
        self.assertAlmostEqual(window.segments[1].left_pct, 70)
        self.assertEqual(len(window.closed_segments), 1)
        self.assertAlmostEqual(window.closed_segments[0].width_pct, 10)

    def test_closed_time_remains_visible_on_the_axis(self):
        window = self.one_day()

        self.assertAlmostEqual(window.pct(self.at(FRIDAY, 14)), 60)
        self.assertAlmostEqual(window.pct(self.at(FRIDAY, 15)), 70)

    def test_an_unscheduled_company_yields_an_empty_window(self):
        calendar = WorkingCalendar.for_company(make_company(name="Sin horario"))
        window = ScheduleWindow.build(calendar, self.at(FRIDAY, 0), self.at(FRIDAY, 23))

        self.assertTrue(window.is_empty)
        self.assertEqual(window.pct(self.at(FRIDAY, 9)), 0.0)
        self.assertIsNone(window.at_offset(10))

    # --- offset / pct ----------------------------------------------------

    def test_offset_uses_real_clock_minutes(self):
        window = self.one_day()

        self.assertEqual(window.offset(self.at(FRIDAY, 8)), 0)
        self.assertEqual(window.offset(self.at(FRIDAY, 10)), 120)
        self.assertEqual(window.offset(self.at(FRIDAY, 14)), 360)
        self.assertEqual(window.offset(self.at(FRIDAY, 16)), 480)
        self.assertEqual(window.offset(self.at(FRIDAY, 18)), 600)

    def test_offset_is_monotonic(self):
        window = self.one_day()
        moments = [self.at(FRIDAY, h, m) for h in range(8, 18) for m in (0, 30)]

        offsets = [window.offset(m) for m in moments]
        self.assertEqual(offsets, sorted(offsets))

    def test_a_moment_inside_a_break_keeps_its_clock_position(self):
        window = self.one_day()

        self.assertEqual(window.offset(self.at(FRIDAY, 14, 30)), 390)

    def test_offsets_clamp_outside_the_window(self):
        window = self.one_day()

        self.assertEqual(window.offset(self.at(FRIDAY, 3)), 0)
        self.assertEqual(window.offset(self.at(FRIDAY, 22)), 600)

    def test_offset_and_at_offset_round_trip(self):
        window = self.one_day()
        rng = random.Random(20260911)

        for _ in range(200):
            minutes = rng.randrange(0, window.total_minutes)
            moment = window.at_offset(minutes)
            self.assertEqual(window.offset(moment), minutes)

    # --- blocks ----------------------------------------------------------

    def test_a_block_spanning_lunch_keeps_its_real_start_and_end(self):
        window = self.one_day()

        _left, width = window.block(self.at(FRIDAY, 13), self.at(FRIDAY, 16))

        self.assertAlmostEqual(width, 30)  # 13:00 to 16:00 on a 10-hour axis

    def test_a_very_short_block_gets_the_minimum_width(self):
        window = self.one_day()

        _left, width = window.block(
            self.at(FRIDAY, 9), self.at(FRIDAY, 9) + timedelta(minutes=3)
        )

        self.assertAlmostEqual(width, 100 * MIN_BLOCK_MINUTES / 600)

    def test_a_block_never_overflows_the_axis(self):
        window = self.one_day()

        left, width = window.block(self.at(FRIDAY, 17, 55), self.at(FRIDAY, 18))

        self.assertLessEqual(left + width, 100.0)

    # --- snapping --------------------------------------------------------

    def test_snap_rounds_down_to_the_grid(self):
        window = self.one_day()

        snapped = window.snap(self.at(FRIDAY, 9, 23))

        self.assertEqual(timezone.localtime(snapped, self.tz).minute, 15)

    def test_snap_uses_real_quarter_hours_during_a_break(self):
        window = self.one_day()

        snapped = window.snap(self.at(FRIDAY, 14, 40))

        self.assertEqual(timezone.localtime(snapped, self.tz).minute, 30)
        self.assertFalse(self.calendar.is_working(snapped))

    def test_ticks_start_each_segment_and_mark_whole_hours(self):
        window = self.one_day()

        labels = [t.label for t in window.ticks()]

        self.assertEqual(labels[0], "08:00")
        self.assertIn("15:00", labels)
        self.assertIn("17:00", labels)
        self.assertIn("14:00", labels)
        self.assertNotIn("19:00", labels)

    def test_now_line_is_none_outside_the_window(self):
        window = self.one_day()

        self.assertIsNone(window.now_pct(self.at(FRIDAY, 6)))
        self.assertIsNotNone(window.now_pct(self.at(FRIDAY, 9)))

    # --- formatting ------------------------------------------------------

    def test_percentages_are_formatted_with_a_dot(self):
        """Spanish locale would render 12,5 and break every inline style."""
        self.assertEqual(fmt_pct(12.5), "12.5000")
        self.assertNotIn(",", fmt_pct(1 / 3 * 100))


class PackTest(TestCase):
    def test_overlapping_blocks_are_pushed_right_and_marked(self):
        blocks = [
            FakeBlock(sequence=0, left_pct=0.0, width_pct=10.0),
            FakeBlock(sequence=1, left_pct=5.0, width_pct=10.0),
        ]

        pack(blocks)

        self.assertFalse(blocks[0].is_displaced)
        self.assertTrue(blocks[1].is_displaced)
        self.assertGreaterEqual(blocks[1].left_pct, blocks[0].left_pct + 10.0)

    def test_blocks_that_already_fit_are_untouched(self):
        blocks = [
            FakeBlock(sequence=0, left_pct=0.0, width_pct=10.0),
            FakeBlock(sequence=1, left_pct=40.0, width_pct=10.0),
        ]

        pack(blocks)

        self.assertEqual([b.left_pct for b in blocks], [0.0, 40.0])
        self.assertFalse(any(b.is_displaced for b in blocks))

    def test_packing_never_overlaps(self):
        blocks = [FakeBlock(sequence=i, left_pct=i * 2.0, width_pct=8.0) for i in range(6)]

        pack(blocks)

        ordered = sorted(blocks, key=lambda b: b.left_pct)
        for left, right in zip(ordered, ordered[1:]):
            self.assertLessEqual(left.left_pct + left.width_pct, right.left_pct + 1e-9)

    def test_an_overflowing_row_is_scaled_back_within_the_axis(self):
        blocks = [FakeBlock(sequence=i, left_pct=0.0, width_pct=30.0) for i in range(6)]

        pack(blocks)

        rightmost = max(b.left_pct + b.width_pct for b in blocks)
        self.assertLessEqual(rightmost, 100.0 + 1e-9)




@dataclass
class FakeOverlapBlock:
    """Stand-in for a UI block; split_overlaps() only touches these."""

    session_id: int
    start: datetime
    left_pct: float
    width_pct: float
    top_pct: float = 0.0
    height_pct: float = 100.0


def _block(session_id: int, left: float, width: float) -> FakeOverlapBlock:
    # start only breaks ties between blocks beginning at the same percentage.
    return FakeOverlapBlock(
        session_id=session_id,
        start=MONDAY + timedelta(minutes=left),
        left_pct=left,
        width_pct=width,
    )


def _lane(block) -> tuple[float, float]:
    return (round(block.top_pct, 3), round(block.height_pct, 3))


class SplitOverlapsTest(TestCase):
    def test_a_block_running_alone_keeps_the_full_row(self):
        alone = _block(1, 10, 20)

        split_overlaps([alone])

        self.assertEqual(_lane(alone), (0, 100))

    def test_two_concurrent_blocks_take_half_the_row_each(self):
        first, second = _block(1, 0, 50), _block(2, 0, 50)

        split_overlaps([first, second])

        self.assertEqual(sorted([_lane(first), _lane(second)]), [(0, 50), (50, 50)])

    def test_three_concurrent_blocks_take_a_third_each(self):
        blocks = [_block(1, 0, 50), _block(2, 0, 50), _block(3, 0, 50)]

        split_overlaps(blocks)

        third = round(100 / 3, 3)
        self.assertEqual(
            sorted(_lane(b) for b in blocks),
            [(0, third), (third, third), (round(200 / 3, 3), third)],
        )

    def test_a_lane_holds_for_the_blocks_whole_length_even_once_alone(self):
        """The old behaviour changed a block's own height mid-flight as
        neighbours came and went. The fixed-lane layout deliberately does not:
        one lane, for the block's entire span, decided once."""
        # A runs 0-40 the whole time; B only overlaps it for 10-30.
        a, b = _block(1, 0, 40), _block(2, 10, 20)

        split_overlaps([a, b])

        self.assertEqual(_lane(a), (0, 50))
        self.assertEqual(_lane(b), (50, 50))

    def test_a_block_outside_any_overlap_is_unaffected_by_a_separate_cluster(self):
        # A and B overlap each other; C is later and touches neither.
        a, b, c = _block(1, 0, 20), _block(2, 10, 20), _block(3, 40, 20)

        split_overlaps([a, b, c])

        self.assertEqual(sorted([_lane(a), _lane(b)]), [(0, 50), (50, 50)])
        self.assertEqual(_lane(c), (0, 100))

    def test_lanes_are_assigned_in_start_order_and_held(self):
        early, late = _block(1, 0, 100), _block(2, 50, 50)

        split_overlaps([late, early])  # deliberately out of order

        self.assertEqual(_lane(early), (0, 50))
        self.assertEqual(_lane(late), (50, 50))

    def test_touching_blocks_do_not_count_as_overlapping(self):
        first, second = _block(1, 0, 50), _block(2, 50, 50)

        split_overlaps([first, second])

        self.assertEqual(_lane(first), (0, 100))
        self.assertEqual(_lane(second), (0, 100))

    def test_a_freed_lane_is_reused_by_the_next_cluster(self):
        # A(0-10) and B(0-10) share lanes 0/1; once both end, C(20-30) alone
        # starts a fresh cluster and gets the full row, not a leftover half.
        a, b, c = _block(1, 0, 10), _block(2, 0, 10), _block(3, 20, 10)

        split_overlaps([a, b, c])

        self.assertEqual(sorted([_lane(a), _lane(b)]), [(0, 50), (50, 50)])
        self.assertEqual(_lane(c), (0, 100))

    def test_running_it_again_recomputes_rather_than_accumulates(self):
        first, second = _block(1, 0, 50), _block(2, 0, 50)
        split_overlaps([first, second])

        split_overlaps([first])

        self.assertEqual(_lane(first), (0, 100))
