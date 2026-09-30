"""Map one working day's datetimes onto horizontal percentages for the calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count

from django.utils import timezone

from production.services.working_calendar import WorkingCalendar, WorkingWindow

# Grid resolution for dragging, matching the old STEP_MINUTES.
SNAP_MINUTES = 15

# Floor for a block's width so a 3-minute stage stays clickable.
MIN_BLOCK_MINUTES = 12

# Gap left between blocks that had to be pushed apart in the per-ticket view.
PACK_GAP_PCT = 0.15


def fmt_pct(value: float) -> str:
    """Format a percentage for a CSS ``style`` attribute.

    Left as a float, Spanish localization renders "12,5%", which browsers
    silently reject -- so this always leaves Python as a string.
    """
    return f"{value:.4f}"


@dataclass(frozen=True)
class Segment:
    """One open stretch, plus where it sits on the axis."""

    start: datetime
    end: datetime
    minutes: int
    offset: int  # wall-clock minutes from the start of the visible day
    left_pct: float
    width_pct: float

    @property
    def left(self) -> str:
        return fmt_pct(self.left_pct)

    @property
    def width(self) -> str:
        return fmt_pct(self.width_pct)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass(frozen=True)
class Tick:
    label: str
    left_pct: float

    @property
    def left(self) -> str:
        return fmt_pct(self.left_pct)


class ScheduleWindow:
    """Wall-clock axis from the day's first opening to its last closing.

    The open segments are retained so templates can shade the closed gaps.
    """

    def __init__(self, windows: list[WorkingWindow], *, tz=None):
        self.tz = tz or timezone.get_current_timezone()
        windows = sorted(
            (window for window in windows if window.minutes > 0),
            key=lambda window: window.start,
        )
        self._start = windows[0].start if windows else None
        self._end = windows[-1].end if windows else None
        self.total_minutes = (
            self._minutes_between(self._start, self._end)
            if self._start and self._end
            else 0
        )

        segments: list[Segment] = []
        for window in windows:
            minutes = window.minutes
            offset = self._minutes_between(self._start, window.start)
            segments.append(
                Segment(
                    start=window.start,
                    end=window.end,
                    minutes=minutes,
                    offset=offset,
                    left_pct=self._as_pct(offset),
                    width_pct=self._as_pct(minutes),
                )
            )
        self.segments = segments

        self.closed_segments: list[Segment] = []
        for previous, following in zip(segments, segments[1:]):
            if previous.end >= following.start:
                continue
            minutes = self._minutes_between(previous.end, following.start)
            offset = self._minutes_between(self._start, previous.end)
            self.closed_segments.append(
                Segment(
                    start=previous.end,
                    end=following.start,
                    minutes=minutes,
                    offset=offset,
                    left_pct=self._as_pct(offset),
                    width_pct=self._as_pct(minutes),
                )
            )

    @classmethod
    def build(
        cls, calendar: WorkingCalendar, start: datetime, end: datetime
    ) -> ScheduleWindow:
        return cls(calendar.windows_between(start, end), tz=calendar.timezone)

    # --- axis ------------------------------------------------------------

    def _as_pct(self, minutes: float) -> float:
        if not self.total_minutes:
            return 0.0
        return 100.0 * minutes / self.total_minutes

    @staticmethod
    def _minutes_between(start: datetime, end: datetime) -> int:
        return int((end.timestamp() - start.timestamp()) // 60)

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def start(self) -> datetime | None:
        return self._start

    @property
    def end(self) -> datetime | None:
        return self._end

    @property
    def min_width_pct(self) -> float:
        return self._as_pct(MIN_BLOCK_MINUTES)

    def offset(self, moment: datetime) -> int:
        """Wall-clock minutes between the visible day start and *moment*."""
        if self.is_empty:
            return 0
        if moment <= self.start:
            return 0
        if moment >= self.end:
            return self.total_minutes
        return self._minutes_between(self.start, moment)

    def pct(self, moment: datetime) -> float:
        return self._as_pct(self.offset(moment))

    def at_offset(self, minutes: float) -> datetime | None:
        """Inverse of :meth:`offset` for the visible wall-clock axis."""
        if self.is_empty:
            return None
        bounded = max(0.0, min(float(minutes), float(self.total_minutes)))
        return self.start + timedelta(minutes=bounded)

    def at_pct(self, percent: float) -> datetime | None:
        return self.at_offset(percent / 100.0 * self.total_minutes)

    def snap(self, moment: datetime, *, minutes: int = SNAP_MINUTES) -> datetime:
        """Round *moment* down to the daily grid."""
        snapped_offset = (self.offset(moment) // minutes) * minutes
        return self.at_offset(snapped_offset) or moment

    def block(self, start: datetime, end: datetime) -> tuple[float, float]:
        """(left_pct, width_pct) for a session, with the minimum width applied."""
        left = self.pct(start)
        width = max(self.pct(end) - left, self.min_width_pct)
        return left, min(width, max(0.0, 100.0 - left))

    def ticks(self) -> list[Tick]:
        """Opening time and every whole hour on the real-time axis."""
        if self.is_empty:
            return []
        marks = [Tick(timezone.localtime(self.start, self.tz).strftime("%H:%M"), 0.0)]
        cursor = timezone.localtime(self.start, self.tz).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        while cursor < self.end:
            marks.append(Tick(cursor.strftime("%H:%M"), self.pct(cursor)))
            cursor += timedelta(hours=1)
        return marks

    def segments_json(self) -> list[dict]:
        """The axis, so the client can propose a snapped datetime while dragging
        without a round-trip per pixel. The server still recomputes on mutation."""
        if self.is_empty:
            return []
        return [
            {
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "offset": 0,
                "minutes": self.total_minutes,
            }
        ]

    def now_pct(self, moment: datetime | None = None) -> float | None:
        """Position of the "now" line, or None when it is off the window."""
        moment = moment or timezone.now()
        if self.is_empty or not (self.start <= moment <= self.end):
            return None
        return self.pct(moment)


# Tolerance for float error in the datetime-to-percent maths.
EPSILON_PCT = 1e-6


def split_overlaps(blocks: list) -> None:
    """Give every block a stable lane, in place: ``top_pct``/``height_pct``.

    A connected run of overlapping blocks splits the row into 1/n lanes for
    the whole cluster's duration, assigned once so a block never resizes
    partway through its own length.
    """
    ordered = sorted(blocks, key=lambda b: (b.left_pct, b.start, b.session_id))
    active: list[tuple[float, int]] = []  # (right edge, lane) still running
    cluster: list[tuple[object, int]] = []
    lanes_used = 0

    def close_cluster():
        if not cluster:
            return
        total = lanes_used + 1
        for block, lane in cluster:
            block.top_pct = lane * 100.0 / total
            block.height_pct = 100.0 / total
        cluster.clear()

    for block in ordered:
        active = [
            (right, lane) for right, lane in active if right > block.left_pct + EPSILON_PCT
        ]
        if not active and cluster:
            close_cluster()
            lanes_used = 0

        taken = {lane for _, lane in active}
        lane = next(candidate for candidate in count() if candidate not in taken)
        lanes_used = max(lanes_used, lane)
        active.append((_right(block), lane))
        cluster.append((block, lane))

    close_cluster()


def _right(block) -> float:
    return block.left_pct + block.width_pct


def pack(blocks: list) -> None:
    """Push overlapping blocks right, in place, for the per-ticket rows.

    Displaced blocks are flagged so the template can show they aren't at their
    true time; ``data-start``/``data-end`` still carry the real values.
    """
    cursor = 0.0
    for block in sorted(blocks, key=lambda b: (b.left_pct, b.sequence)):
        if block.left_pct < cursor:
            block.left_pct = cursor
            block.is_displaced = True
        cursor = block.left_pct + block.width_pct + PACK_GAP_PCT

    if cursor > 100.0:
        scale = 100.0 / cursor
        for block in blocks:
            block.left_pct *= scale
            block.width_pct *= scale
