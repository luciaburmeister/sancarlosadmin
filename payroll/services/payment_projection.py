"""
Per-employee payroll payment date projection.

Resolves the day of the month an individual employee's payroll is expected
to be represented in the Cashflow, taking into account a possible
per-employee override of the company's default payment day.
"""

from __future__ import annotations

import calendar
from datetime import date

from payroll.models.nomina import EmployeePayroll
from payroll.services.period_service import _last_business_day


def get_projected_payment_date(employee_payroll: EmployeePayroll) -> date:
    """
    Resolves the day of the month this employee's payroll is projected to
    appear in the cashflow: their own payment_day_override if set,
    otherwise the company's configured salary_payment_day (0 meaning "last
    business day of the month").

    This is a DISPLAY/PROJECTION date for cashflow purposes only — it is
    NOT a claim that the bank actually executes the payment on this exact
    day. Real payment timing varies (typically end of month) and is
    confirmed separately, once someone verifies the payment went out. Do
    not use this function's result to determine whether a payment has
    actually happened.
    """
    period = employee_payroll.period
    config = getattr(period.company, "payroll_config", None)
    day = employee_payroll.payment_day_override or getattr(config, "salary_payment_day", 0) or 0

    if day and 1 <= day <= 31:
        last_day_of_month = calendar.monthrange(period.year, period.month)[1]
        return date(period.year, period.month, min(day, last_day_of_month))

    return _last_business_day(period.year, period.month)
