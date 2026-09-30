from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.utils import timezone

# next_open() gives up after this many days rather than looping forever.
MAX_LOOKAHEAD_DAYS = 60


def _aware(day: date, moment: time, tz) -> datetime:
    """Combine *day* and *moment* into an aware datetime, resolving DST edges.

    A round trip through UTC detects a non-existent spring-forward time (the
    wall clock comes back different); ``fold=0`` picks the pre-transition
    occurrence on a fall-back repeat.
    """
    naive = datetime.combine(day, moment)
    aware = naive.replace(tzinfo=tz, fold=0)

    round_tripped = aware.astimezone(dt_timezone.utc).astimezone(tz).replace(tzinfo=None)
    if round_tripped != naive:
        # Non-existent local time: skip forward by the size of the jump.
        gap = round_tripped - naive
        aware = (naive + gap).replace(tzinfo=tz, fold=0)

    return aware


def _elapsed_minutes(start: datetime, end: datetime) -> int:
    """Real elapsed minutes between two aware datetimes.

    Normalised to UTC first: subtracting same-tzinfo datetimes ignores offsets
    and is off by an hour across a DST boundary.
    """
    delta = end.astimezone(dt_timezone.utc) - start.astimezone(dt_timezone.utc)
    return int(delta.total_seconds() // 60)


def _merge_ranges(ranges: list[tuple[time, time]]) -> list[tuple[time, time]]:
    """Sort and coalesce abutting/overlapping ``(start, end)`` time ranges."""
    merged: list[tuple[time, time]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True)
class WorkingWindow:
    """One contiguous open range. ``start`` and ``end`` are aware datetimes."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        """Real elapsed minutes, DST-correct."""
        return _elapsed_minutes(self.start, self.end)

    def clipped(self, start: datetime, end: datetime) -> WorkingWindow | None:
        """This window restricted to ``[start, end)``, or None if disjoint."""
        lo = max(self.start, start)
        hi = min(self.end, end)
        return WorkingWindow(lo, hi) if lo < hi else None

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


class WorkingCalendar:
    """Open working windows for a company, derived from its schedule slots.

    Instances are cheap value objects over an already-loaded slot list; build
    one per request with :meth:`for_company` and pass it around rather than
    re-querying.
    """

    def __init__(self, slots_by_weekday: dict[int, list[tuple[time, time]]], *, tz=None):
        self._slots = {wd: _merge_ranges(ranges) for wd, ranges in slots_by_weekday.items()}
        self._tz = tz or timezone.get_current_timezone()

    @classmethod
    def for_company(cls, company, *, tz=None) -> WorkingCalendar:
        by_weekday: dict[int, list[tuple[time, time]]] = defaultdict(list)
        for slot in company.schedule_slots.all():
            by_weekday[slot.weekday].append((slot.start_time, slot.end_time))
        return cls(dict(by_weekday), tz=tz)

    @property
    def timezone(self):
        return self._tz

    @property
    def has_slots(self) -> bool:
        """False when the company has no schedule configured at all."""
        return any(self._slots.values())

    def windows_for_date(self, day: date) -> list[WorkingWindow]:
        """Open windows on *day*, in chronological order. Empty on a rest day."""
        return [
            WorkingWindow(_aware(day, start, self._tz), _aware(day, end, self._tz))
            for start, end in self._slots.get(day.weekday(), ())
        ]

    def windows_between(self, start: datetime, end: datetime) -> list[WorkingWindow]:
        """Open windows intersecting ``[start, end)``, clipped to those bounds."""
        if start >= end:
            return []

        windows: list[WorkingWindow] = []
        # Extra day on each side covers timezone conversion drift.
        first = timezone.localtime(start, self._tz).date() - timedelta(days=1)
        last = timezone.localtime(end, self._tz).date() + timedelta(days=1)

        day = first
        while day <= last:
            for window in self.windows_for_date(day):
                clipped = window.clipped(start, end)
                if clipped is not None:
                    windows.append(clipped)
            day += timedelta(days=1)

        windows.sort(key=lambda w: w.start)
        return windows

    def working_minutes_between(self, start: datetime, end: datetime) -> int:
        """Open minutes in ``[start, end)`` - closed time excluded."""
        return sum(w.minutes for w in self.windows_between(start, end))

    def is_working(self, moment: datetime) -> bool:
        day = timezone.localtime(moment, self._tz).date()
        return any(w.contains(moment) for w in self.windows_for_date(day))

    def next_open(self, moment: datetime) -> datetime | None:
        """First open instant at or after *moment*, or None within the lookahead.

        Returns *moment* itself when it already falls inside a window.
        """
        if not self.has_slots:
            return None

        day = timezone.localtime(moment, self._tz).date()
        for _ in range(MAX_LOOKAHEAD_DAYS + 1):
            for window in self.windows_for_date(day):
                if window.contains(moment):
                    return moment
                if window.start >= moment:
                    return window.start
            day += timedelta(days=1)

        return None
