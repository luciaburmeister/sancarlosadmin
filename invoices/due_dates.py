"""
When a sales invoice falls due: ``date_issued`` + the payment terms (days),
moved forward to the client's next payment day when it only pays on fixed
days of the month (e.g. the 10th and the 25th).

This module is the only place that knows the rule: derive a due date with
``compute_due_date()``. A payment day past the end of a month (31 in April)
counts as that month's last day.
"""
import calendar
from datetime import date, timedelta


def _payment_day_in(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def compute_due_date(date_issued: date, terms_days: int, payment_days=()) -> date:
    """``date_issued`` + ``terms_days``, moved to the next payment day (if any) on or after it."""
    base = date_issued + timedelta(days=terms_days or 0)
    days = sorted(set(payment_days or ()))
    if not days:
        return base

    year, month = base.year, base.month
    for _ in range(2):
        for day in days:
            candidate = _payment_day_in(year, month, day)
            if candidate >= base:
                return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    # Unreachable: the following month always has a candidate after base
    return base
