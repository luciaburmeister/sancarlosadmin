"""Shared calendar-arithmetic helpers used across apps."""

from __future__ import annotations

from datetime import date, timedelta


def week_bounds(day: date) -> tuple[date, date]:
    """Return (monday, sunday) for the ISO week containing *day*."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)
